# SC4S in Production: What Broke, Why I Changed the Architecture, and How I Fixed It

**A field guide to Splunk Connect for Syslog, written from a real FortiGate and Bitdefender migration rather than from a clean lab**

Published: August 24, 2026
Revised: September 4, 2026
Author: Majid Ershadi

> **Version note.** This article is about SC4S 3.x and the architecture around it, not about memorizing one release's internal implementation. I used SC4S 3.45.1 during this work. SC4S changes, so check the current documentation and the `splunk_metadata.csv.example` shipped with the exact image you run before copying parser keys or tuning values into production.

This work started with a fairly ordinary failure: my FortiGate was sending live syslog over UDP into a Splunk Heavy Forwarder, and after a while the forwarder stopped forwarding. Restarting Splunk brought the flow back, but it also exposed the part that worried me. While the receiver was down, the firewall kept producing UDP logs and those logs had nowhere durable to wait.

That was the point where this stopped being a "restart Splunk" problem and became an ingestion-architecture problem.

The migration that followed was not clean. I hit an air-gapped Docker failure, HEC index authorization errors, SC4S metadata defaults I initially misunderstood, a timezone setting copied from somewhere it did not belong, and a Bitdefender JSON feed that looked fine on the wire but stopped parsing correctly after SC4S was inserted.

Those mistakes ended up being more useful than a perfect installation guide. They forced me to understand where each decision belongs:

```text
transport
    != framing
    != source identification
    != raw-message formatting
    != Splunk metadata
    != Splunk parsing
    != search-time field extraction
```

What follows is the record I wish I had while doing the migration: the options I considered, the configuration that held up, the mistakes that cost time, and the checks that finally made each boundary understandable. It also covers custom parsing, Splunk's indexing pipeline, `SEDCMD`, high-load tuning, macvlan, buffering, and the method I now use to follow one event from the source device to a searchable Splunk event.

The primary references are the current [SC4S documentation](https://splunk.github.io/splunk-connect-for-syslog/main/), the [SC4S project repository](https://github.com/splunk/splunk-connect-for-syslog), [Splunk's data-pipeline documentation](https://help.splunk.com/en/splunk-enterprise/administer/distributed-deployment-manual/10.4/overview-of-splunk-enterprise-distributed-deployments/how-data-moves-through-splunk-deployments-the-data-pipeline), and [AxoSyslog documentation](https://axoflow.com/docs/axosyslog-core/).

---

## The original problem was not "syslog is broken"

The first design was simple:

```text
FortiGate
   |
   | UDP syslog
   v
Splunk Heavy Forwarder
   |
   | Splunk forwarding
   v
Indexer cluster
```

There is nothing automatically invalid about this.

A Heavy Forwarder is a full Splunk Enterprise instance. It can receive data, parse it, apply `props.conf` and `transforms.conf`, route it, and forward parsed events onward. Splunk documents the HF specifically for cases where event-level processing or routing is needed before the indexing tier.

The problem was the failure mode.

When my HF stopped forwarding, restarting it recovered the process but not the UDP events generated while the receiver was unavailable.

That distinction matters:

```text
process recovery != data recovery
```

UDP does not wait for your collector to come back. A packet can be emitted successfully from the device and still disappear somewhere before the collector application handles it.

The requirement therefore became:

> I need a collection layer whose first job is receiving syslog reliably enough to survive downstream outages and maintenance, rather than making the Splunk parsing process itself the network listener.

That requirement is what led me to SC4S.

Reference: [Splunk forwarder types](https://help.splunk.com/en/data-management/forward-data/forwarding-and-receiving-data/10.4.2604/introduction-to-forwarding/types-of-forwarders)

## I considered four ways forward

Before choosing SC4S, I considered four architectures.

### Option 1 — Keep the Heavy Forwarder and troubleshoot it

This was the least disruptive option.

The HF stopping could have been caused by:

- blocked `tcpout` queues;
- an indexer-side problem;
- output connection failures;
- TLS state;
- disk pressure;
- a downstream destination blocking the forwarding pipeline.

Splunk's `metrics.log` is useful here because queue entries can show `blocked=true` and sustained queue occupancy.

I still consider that investigation valuable. A migration should not become an excuse to ignore the original failure.

But even if I fixed the HF perfectly, one limitation remained: the source was still sending live UDP directly into a process I occasionally needed to restart.

So fixing the HF did not remove the architectural weakness.

Useful Splunk checks:

```spl
index=_internal host="<HF>"
source="*metrics.log"
group=queue
| timechart max(current_size) by name
```

```spl
index=_internal host="<HF>"
source="*splunkd.log"
(
    "TcpOutputProc"
    OR "blocked"
    OR "Connection to host"
    OR "SSL"
)
```

Reference: [Splunk metrics.log](https://help.splunk.com/data-management/monitor-and-troubleshoot/troubleshoot-splunk-enterprise/9.1/splunk-enterprise-log-files/about-metrics.log)

### Option 2 — Plain syslog-ng, write files, then Universal Forwarder

This is still one of the strongest traditional designs:

```text
device
   |
   v
syslog-ng
   |
   v
durable local files
   |
   v
Universal Forwarder
   |
   v
Splunk
```

I like this architecture when durable raw files are a requirement.

It separates collection from forwarding cleanly. If Splunk is unavailable, syslog-ng can continue writing. The UF resumes from files later.

The cost is operational ownership. I would need to maintain:

- vendor classification;
- directory layout;
- file rotation;
- sourcetypes;
- metadata;
- parsing compatibility;
- Splunk forwarding;
- possibly a large number of source-specific rules.

It is a good design. It was simply not the design I wanted for a Splunk-heavy security environment with many vendor syslog sources.

### Option 3 — Plain syslog-ng directly to HEC

Also possible.

syslog-ng is capable of HTTP destinations, buffering, parsing, rewrites, and templates. If I used it directly, I would have maximum freedom.

But I would also own the Splunk integration contract myself.

Every time I onboarded a Fortinet, Cisco, DLP, WAF, AV, proxy, or other device, I would be deciding from scratch:

```text
what is this source?
what sourcetype should it use?
what index?
what timestamp?
what should _raw look like?
what fields should be passed through HEC?
what TA expects this data?
```

That is manageable for a few sources. It becomes a local product over time.

### Option 4 — SC4S

SC4S gave me the syslog engine plus a Splunk-oriented source catalog and metadata model.

The project exists specifically to reduce several recurring Splunk/syslog problems:

- catch-all `syslog` sourcetypes;
- inconsistent syslog servers;
- lack of deep syslog expertise;
- uneven Splunk indexer distribution;
- repeated source-specific onboarding work.

That matched my problem better than building another bespoke collector.

So I chose SC4S.

Reference: [SC4S project purpose](https://github.com/splunk/splunk-connect-for-syslog)

## The architecture I chose — and the compromise I kept

The first working migration path became:

```text
FortiGate
   |
   | UDP/5514 for the initial migration
   v
SC4S
   |
   | HTTPS / HEC 8088
   v
Heavy Forwarder
   |
   | Splunk-to-Splunk
   v
Indexer cluster
```

Later I added Bitdefender:

```text
Bitdefender
   |
   | TCP/1514
   v
SC4S
   |
   | HEC
   v
Heavy Forwarder
   |
   v
Indexer cluster
```

I need to be clear about one design choice here.

Current SC4S guidance prefers sending HEC directly to the Splunk indexer HEC tier rather than inserting an HF just to relay the data.

The cleaner long-term design is:

```text
sources -> SC4S -> HEC VIP / indexers -> indexer cluster
```

Why did I keep the HF?

Because I was migrating an existing environment incrementally. The HF was already an accepted Splunk-side receiving point, and changing both the source collection architecture and the final Splunk ingress topology at the same time would have made troubleshooting harder.

That is a migration decision, not a claim that the intermediate HF is SC4S best practice.

The rule I use is:

> Keep an HF in the path only when it performs a function you actually need.

Examples include mandatory routing, masking, controlled network segmentation, or existing intermediate-tier policy.

If it only receives HEC and forwards unchanged data, it is another failure domain.

References:

- [SC4S Splunk setup](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-splunk-setup/)
- [Splunk intermediate forwarding architecture](https://help.splunk.com/en/splunk-enterprise/splunk-validated-architectures/getting-data-in-forwarding-and-preprocessing/intermediate-data-routing-using-universal-and-heavy-forwarders)

## The mental model that made the rest easier

I had to stop treating "syslog" as one thing.

A better model is six layers.

### Transport

Examples:

```text
UDP
TCP
TLS
```

Transport answers:

> How do bytes get from the source to the collector?

### Framing

TCP is a byte stream. It needs a way to tell where one event ends and another begins.

RFC6587 is one framing method commonly used for reliable syslog.

Framing answers:

> Where are the message boundaries?

### Syslog envelope

Examples:

```text
RFC3164-like
RFC5424
vendor-broken RFC3164
```

This contains things such as PRI, timestamp, hostname, program, and structured data.

### Vendor message body

Examples:

```text
FortiOS key=value text
CEF
JSON
LEEF
custom appliance text
```

### SC4S metadata

Examples:

```text
index
sourcetype
host
source
template
vendor
product
```

### Splunk processing

Examples:

```text
INDEXED_EXTRACTIONS
LINE_BREAKER
TIME_FORMAT
TRANSFORMS
SEDCMD
KV_MODE
REPORT
EXTRACT
FIELDALIAS
```

Those layers interact, but they are not interchangeable.

A TCP port does not define a Splunk index.

A HEC token does not identify a vendor.

A sourcetype does not make malformed JSON valid.

That sounds obvious after the fact. Several of my troubleshooting mistakes came from crossing those boundaries mentally.

## UDP versus TCP: the migration decision

I intentionally did not change FortiGate from UDP to TCP on day one.

My first goal was to prove:

```text
FortiGate -> SC4S -> HEC -> Splunk
```

without changing both collector and source transport simultaneously.

So phase one remained:

```text
FortiGate -> UDP/5514 -> SC4S
```

Once the complete ingestion path was stable, the next planned improvement was reliable TCP/RFC6587 where the FortiOS version and framing behavior had been tested.

This is an important migration principle:

> Change one failure domain at a time when you need to know which change caused the result.

TCP is usually preferable when messages are large or when flow control matters. SC4S documentation specifically calls out DLP, IDS, proxy, and similar large-event sources as cases where TCP can be a better fit.

But TCP is not exactly-once delivery.

TCP can still lose data around:

- connection establishment;
- sender queue exhaustion;
- process restart;
- framing errors;
- application-level rejection.

SC4S documentation is quite direct about this: syslog can only be made "mostly available."

Reference: [SC4S architecture: UDP vs TCP](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)

## First deployment decision: do not immediately uninstall the old syslog daemon

The Ubuntu host already had syslog-ng.

My first instinct was to remove it because SC4S includes its own syslog engine.

I decided against deleting it during migration.

The safer sequence was:

```bash
systemctl stop syslog-ng
systemctl disable syslog-ng
systemctl mask syslog-ng
```

Why?

Because the immediate problem was port ownership, not package existence.

Keeping the old configuration gave me a rollback path if SC4S failed before the migration was proven.

I also did not disable `systemd-journald`. Host journaling and network syslog reception are separate responsibilities.

Before starting SC4S I checked the listener state:

```bash
ss -lntup
```

and specifically:

```bash
ss -lntup | grep -E ':(514|5514|601|6514|8080|8088)\b'
```

The practical lesson:

> Remove conflicts first. Remove software later.

## The air-gapped failure: the container was local, but systemd still wanted the internet

This was one of the more useful failures because the error looked like an SC4S startup problem but had nothing to do with SC4S configuration.

The service failed with:

```text
failed to resolve reference
ghcr.io/splunk/splunk-connect-for-syslog/container3@sha256:...
dial tcp ...:443: i/o timeout
```

The host was air-gapped.

The image already existed locally.

The problem was this kind of service line:

```ini
ExecStartPre=/usr/bin/docker pull ${SC4S_IMAGE}
```

Every restart tried to contact GHCR before starting the local image.

That is wrong for an offline system.

I tagged the already loaded image locally:

```bash
docker tag \
  ghcr.io/splunk/splunk-connect-for-syslog/container3@sha256:<digest> \
  sc4slocal:3.45.1
```

Then changed the service to use:

```ini
Environment="SC4S_IMAGE=sc4slocal:3.45.1"
```

removed the `docker pull`, and forced:

```text
--pull=never
```

The important part is not the exact tag name.

It is the lifecycle:

```text
connected staging system
   |
   | obtain approved image
   | verify digest
   | docker save / official offline archive
   v
controlled transfer
   |
   v
air-gapped host
   |
   | verify checksum
   | docker load
   | local approved tag
   v
systemd --pull=never
```

For an air-gapped collector, an ordinary service restart should never depend on an external registry.

That sounds obvious. It is easy to miss when copying an online systemd example.

## HEC bootstrap: the first "Incorrect index"

The next problem was HEC.

My first token was designed for Bitdefender:

```ini
[http://sc4s_av]
disabled = 0
index = av
indexes = av
useACK = 0
```

SC4S started and immediately tested its HEC destination using its own operational/fallback events.

Those tests target `main`.

Splunk returned:

```json
{"text":"Incorrect index","code":7}
```

At first glance this can look like a token, URL, or TLS problem.

It was simpler:

```text
SC4S startup event -> main
HEC token allowed -> av only
```

Adding `main` to the token authorization fixed the startup check:

```ini
indexes = av,main
```

This taught me an important distinction in HEC configuration.

### `index =` is a default

Example:

```ini
index = av
```

means:

> If the HEC event does not specify an index, use `av`.

### `indexes =` is authorization

Example:

```ini
indexes = av,main
```

means:

> This token is permitted to submit to these indexes.

### Event-level HEC metadata wins

SC4S normally sends:

```json
{
  "index": "fgt",
  "sourcetype": "fortigate_traffic",
  "event": "..."
}
```

That explicit `"index":"fgt"` is what Splunk will try to use.

The token's `index=av` does not force the event back to `av`.

This distinction later explained the FortiGate failure too.

## TLS warning versus HEC failure: do not debug the loudest message first

At the same time I saw:

```text
ca-cert-trusted.pem does not contain exactly one certificate or CRL: skipping
```

It was tempting to focus on the certificate.

But I tested HEC independently:

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://SplunkServerDefaultCert:8088/services/collector/health
```

and got:

```json
{"text":"HEC is healthy","code":17}
```

Then I submitted events manually to both `main` and the target data index and got:

```json
{"text":"Success","code":0}
```

That evidence changed the diagnosis.

The HTTPS path was working.

The `Incorrect index` response came from the HEC application after the TLS session had already succeeded.

The lesson:

> Separate transport/certificate validation from application authorization.

A noisy certificate warning can coexist with a completely different HEC routing error.

For production I still want certificate verification enabled:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=yes
```

and a trust file containing the correct issuing CA chain.

But I do not use `TLS_VERIFY=no` as a permanent way to make certificate problems disappear.

## FortiGate: tcpdump proved packets arrived, but Splunk still had nothing

After SC4S was running I configured FortiGate to send UDP to port 5514.

`tcpdump` showed traffic arriving.

That proved only this:

```text
FortiGate -> NIC
```

It did not prove:

```text
NIC -> socket -> SC4S parser -> HEC -> Splunk
```

This is why I now treat `tcpdump` as a boundary test, not an end-to-end test.

I checked:

```bash
ss -lunp | grep ':5514'
```

and SC4S statistics:

```bash
docker exec SC4S syslog-ng-ctl stats
```

Eventually the useful evidence appeared in SC4S's HEC error:

```json
{
  "sourcetype":"fortigate_traffic",
  "index":"netfw",
  "host":"fgt-01"
}
```

followed by:

```json
{"text":"Incorrect index","code":7}
```

That was the moment the architecture became clear.

SC4S had correctly identified FortiGate.

It had correctly assigned the sourcetype.

It had also assigned its **default SC4S index**:

```text
netfw
```

My organization wanted:

```text
fgt
```

And the HEC token allowed only my chosen indexes.

So the parser was correct. The governance policy was different.

Reference: [SC4S Fortinet FortiOS source](https://splunk.github.io/splunk-connect-for-syslog/main/sources/vendor/Fortinet/fortios/)

## SC4S default indexes are recommendations, not mandatory names

This is worth stating directly because I initially treated the defaults as more authoritative than they are.

SC4S ships with a practical taxonomy:

```text
netfw
netops
netdlp
netids
epav
...
```

These make onboarding easier.

They are not a requirement.

If your data model says:

```text
FortiGate  -> fgt
Bitdefender -> av
Cisco      -> cisco
DLP        -> dlp
```

that is valid.

SC4S documentation explicitly supports metadata overrides.

I used:

```csv
fortinet_fortios_traffic,index,fgt
fortinet_fortios_utm,index,fgt
fortinet_fortios_event,index,fgt
fortinet_fortios_log,index,fgt
```

in:

```text
/opt/sc4s/local/context/splunk_metadata.csv
```

Then the HEC token allowed:

```ini
indexes = main,fgt,av
```

Now the responsibilities were clean:

```text
SC4S internal events -> main
FortiGate            -> fgt
Bitdefender           -> av
```

The current SC4S documentation recommends treating `splunk_metadata.csv` as a true override file. Do not copy the entire `.example` file into it.

Use the `.example` file as the version-specific reference:

```bash
grep '^fortinet_fortios_' \
  /opt/sc4s/local/context/splunk_metadata.csv.example
```

Reference: [SC4S metadata configuration](https://splunk.github.io/splunk-connect-for-syslog/latest/configuration/)

## Why I override index freely but sourcetype cautiously

Index is an organizational decision.

Sourcetype is often an application contract.

For example:

```text
fortigate_traffic
```

may be tied to a Fortinet TA's:

- field extractions;
- aliases;
- tags;
- eventtypes;
- CIM mapping;
- dashboards.

Changing:

```csv
fortinet_fortios_traffic,index,fgt
```

usually changes only data placement.

Changing:

```csv
fortinet_fortios_traffic,sourcetype=my_firewall
```

can disconnect the data from the TA.

SC4S documentation warns that sourcetype and template overrides affect upstream TA behavior.

My rule is:

> Customize indexes to fit governance. Preserve vendor sourcetypes unless you know exactly why you are changing them.

## Another small mistake: the timezone copied from somebody else's configuration

My `env_file` contained:

```ini
SC4S_DEFAULT_TIMEZONE=Asia/Tehran
```

It was not part of my design. It had been copied from another example.

The FortiGate event itself already included:

```text
tz="+0330"
```

and SC4S was correctly converting event time.

The global timezone was unnecessary and potentially dangerous for another source that lacked timezone information.

I removed it.

This looks trivial compared with HEC failures, but configuration drift often comes from exactly this kind of line.

Best practice:

> If you cannot explain why an environment variable exists in your collector, remove it or document it before production.

## One SC4S can route many sources to many indexes

Once FortiGate worked, the next architecture question was whether every new source needed:

```text
another SC4S
another port
another HEC token
```

No.

These are separate concerns.

A single SC4S can receive:

```text
FortiGate
Cisco
DLP
Bitdefender
WAF
Linux
...
```

and assign different per-event metadata before sending everything through one HEC destination.

Conceptually:

```text
FortiGate ----\
Cisco ---------\
DLP ------------> SC4S ---> one HEC endpoint/token ---> Splunk
Bitdefender ----/
               |
               +--> index=fgt
               +--> index=cisco
               +--> index=dlp
               +--> index=av
```

The HEC token simply needs permission for those indexes if you use an allow-list.

A separate HEC token or alternate SC4S HEC destination makes sense when I need real isolation:

- different Splunk deployments;
- different trust domains;
- different credentials;
- different retention/security boundaries;
- independent destinations.

Not merely because one source goes to another index.

Reference: [SC4S destinations](https://splunk.github.io/splunk-connect-for-syslog/main/destinations/)

## Incoming ports are not routing policy

FortiGate was on UDP/5514.

Bitdefender used TCP/1514.

A future DLP might use another TCP port.

That does not mean:

```text
5514 -> HEC A
1514 -> HEC B
1515 -> HEC C
```

A port is usually a **source-ingress decision**.

The index is a **metadata decision**.

For supported sources SC4S may identify vendors on shared/default listeners.

Unique ports are useful when:

- the product requires one;
- the message cannot be distinguished safely;
- I use SIMPLE;
- I need protocol/framing isolation;
- firewall policy benefits from separation.

SC4S supports unique source ports, and SIMPLE explicitly requires a unique port per SIMPLE source.

Reference: [SC4S SIMPLE source](https://splunk.github.io/splunk-connect-for-syslog/develop/sources/simple/)

## Bitdefender exposed a different class of problem: the event arrived, but its shape changed

Before SC4S, Bitdefender sent JSON to the Heavy Forwarder and the data parsed correctly.

The important property was:

```text
_raw starts with {
```

After I inserted SC4S:

```text
Bitdefender -> SC4S -> HEC -> HF
```

the events still arrived, but fields were no longer extracted correctly.

This was not a network problem.

It was not an index problem.

It was not a HEC token problem.

The raw event contract had changed.

### The first fix: `t_msg_trim`

My SIMPLE metadata became:

```csv
bitdefender_gz,index,av
bitdefender_gz,sourcetype,bitdefender:gz
bitdefender_gz,sc4s_template,t_msg_trim
```

SC4S defines:

```text
t_msg_only = ${MSGONLY}
t_msg_trim = $(strip $MSGONLY)
```

That removes the syslog envelope and strips surrounding whitespace from the message body.

For some Bitdefender events—such as the license usage messages—that was enough.

They reached Splunk as clean JSON again.

Reference: [SC4S output templates](https://splunk.github.io/splunk-connect-for-syslog/latest/configuration/)

## Why `t_msg_trim` did not fix `[av]`, `[uc]`, and the other Bitdefender event classes

Some Bitdefender messages still reached Splunk like:

```text
[av] {...JSON...}
```

or:

```text
[uc] {...JSON...}
```

Other observed prefixes included:

```text
[hd]
[modules]
[antitampering]
[application-inventory]
```

`t_msg_trim` was not failing.

It was doing exactly what it promises: trimming whitespace around `MSGONLY`.

Those prefixes were part of the message body.

So:

```text
MESSAGE = [av] {"field":"value"}
```

became:

```text
[av] {"field":"value"}
```

not:

```json
{"field":"value"}
```

That distinction is important.

A template decides **which macros/body are emitted**.

A rewrite changes **the content of a field**.

I needed a rewrite.

## The Bitdefender post-filter I ended up using

Because Bitdefender had its own dedicated TCP/1514 SIMPLE path, I could scope the rewrite tightly.

The local SC4S file:

```text
/opt/sc4s/local/config/app_parsers/rewriters/app-bitdefender-strip-prefix.conf
```

contained:

```conf
block parser app-postfilter-bitdefender-strip-prefix() {
    channel {
        rewrite {
            subst(
                '^\[(av|uc|hd|modules|antitampering|application-inventory)\][[:space:]]*',
                "",
                value("MESSAGE")
            );
        };
    };
};

application app-postfilter-bitdefender-strip-prefix[sc4s-postfilter] {
    filter {
        match(
            "1514",
            value("fields.sc4s_destport")
            type(glob)
        )
        and message(
            '^\[(av|uc|hd|modules|antitampering|application-inventory)\][[:space:]]*'
        );
    };

    parser {
        app-postfilter-bitdefender-strip-prefix();
    };
};
```

I deliberately used an allow-list of known prefixes instead of:

```regex
^\[[^]]+\]
```

Why?

Because I do not want a future Bitdefender message with a new bracketed semantic marker to be silently altered before I understand it.

The scope is also narrow:

```text
destination port 1514
AND
known Bitdefender prefix
```

That makes accidental cross-vendor rewriting much less likely.

SC4S documents local post-filters and shows `fields.sc4s_destport` as a valid discriminator for this type of local rewrite.

Reference: [SC4S troubleshooting/custom post-filter examples](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/troubleshooting/troubleshoot_resources.md)

## A validation trap: `syslog-ng --syntax-only` failed even though the problem was elsewhere

I tried:

```bash
docker exec SC4S syslog-ng --syntax-only
```

and got:

```text
/conf.d/sc4slib/global_options/plugin.py: not found
confgen: Generator program returned with non-zero exit code
```

This looked like a syntax error in my custom file.

It was not.

SC4S builds parts of its configuration through its entrypoint/confgen environment. Running the bare syslog-ng binary inside the already-running container does not reproduce the complete startup generation context.

The supported practical validation path is SC4S's own restart/preflight:

```bash
systemctl restart sc4s
journalctl -u sc4s --since "2 minutes ago" --no-pager
```

Then inspect the effective preprocessed configuration:

```bash
docker exec SC4S \
  syslog-ng-ctl config --preprocessed \
  | grep -n -A30 -B5 \
    'app-postfilter-bitdefender-strip-prefix'
```

This was another useful lesson:

> A syntax-check command is only useful if it runs in the same configuration-generation context as the actual service.

## The most important boundary: SC4S parsing versus Splunk parsing

The Bitdefender problem forced me to revisit where parsing actually happens.

The complete path in my current topology is:

```text
Bitdefender / FortiGate
        |
        v
SC4S
--------------------------------------------------
syslog envelope parsing
source identification
SC4S parser
SC4S post-filter / rewrite
SC4S output template
HEC metadata construction
        |
        v
HEC input on Heavy Forwarder
--------------------------------------------------
Splunk input phase
Splunk structured parsing
Splunk parsing
Splunk indexing metadata / routing
        |
        v
Heavy Forwarder sends parsed/cooked data
        |
        v
Indexer cluster
--------------------------------------------------
index write
        |
        v
Search tier
--------------------------------------------------
search-time field extraction / knowledge
```

A Heavy Forwarder parses data before forwarding it. Splunk documents heavy-forwarder output as parsed/cooked data.

This means that in this topology, ingest-time TA settings belong on the HF.

I cannot assume an indexer will take parsed/cooked data from the HF and repeat the original structured parsing work.

References:

- [Splunk heavy forwarders parse before forwarding](https://help.splunk.com/en/data-management/forward-data/forwarding-and-receiving-data/10.4.2604/introduction-to-forwarding/types-of-forwarders)
- [Splunk structured forwarded data caveat](https://help.splunk.com/en/splunk-enterprise/forward-and-process-data/forwarding-and-receiving-data/9.1/perform-advanced-configuration/route-and-filter-data)

## Splunk's parsing order — and where `SEDCMD` actually sits

This matters enough to write down explicitly.

Splunk documents the major configuration phases in order.

A useful simplified view is:

```text
INPUT
  inputs.conf
  basic input metadata
        |
        v
STRUCTURED PARSING
  INDEXED_EXTRACTIONS
  structured-data header extraction
        |
        v
PARSING
  LINE_BREAKER / line merging
  timestamp extraction
  TRANSFORMS-
  SEDCMD
        |
        v
INDEXING
        |
        v
SEARCH
  KV_MODE
  REPORT-
  EXTRACT-
  FIELDALIAS-
  EVAL-
  LOOKUP-
```

The exact internals are more complex—Splunk notes that parsing itself contains parsing, merging, and typing pipelines—but the configuration-order point above is important for troubleshooting.

Reference: [Splunk configuration parameters and the data pipeline](https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/10.2/administer-splunk-enterprise-with-configuration-files/configuration-parameters-and-the-data-pipeline)

## Why `SEDCMD` was not my first choice for fixing Bitdefender

I could have tried this on the Heavy Forwarder:

```ini
[bitdefender:gz]
SEDCMD-strip-bd-prefix = s/^\[(av|uc)\]\s*//
```

That is a valid class of Splunk ingest-time rewrite.

But the processing order matters.

Suppose the TA uses:

```ini
INDEXED_EXTRACTIONS = JSON
```

Splunk performs `INDEXED_EXTRACTIONS` in the **structured parsing phase**.

`SEDCMD` comes later in the **parsing phase**.

If the event arrives as:

```text
[av] {"field":"value"}
```

then JSON structured extraction can already have failed before `SEDCMD` gets a chance to remove `[av]`.

That is why upstream cleanup in SC4S is more deterministic when SC4S itself introduced or preserved the prefix around a payload that the downstream TA expects to be pure JSON.

There is another ordering consequence:

Splunk lists `TRANSFORMS` before `SEDCMD`.

So if a `TRANSFORMS-` regex needs the cleaned content, relying on a later `SEDCMD` can also be the wrong order.

`SEDCMD` is not bad.

It is simply not "the first regex that edits `_raw`."

Reference: [Splunk data-pipeline parameter order](https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/10.2/administer-splunk-enterprise-with-configuration-files/configuration-parameters-and-the-data-pipeline)

## When `SEDCMD` is a good fit

I still use this decision rule.

Use `SEDCMD` when:

- the correction clearly belongs to Splunk's parsing tier;
- the event is already at the correct sourcetype;
- earlier structured parsing does not depend on the unmodified body;
- the rewrite should be managed as part of Splunk TA/parsing configuration;
- the source collector should remain transparent.

Example:

```ini
[my:sourcetype]
SEDCMD-remove-noise = s/^UNWANTED://
```

Do not use it automatically when:

- `INDEXED_EXTRACTIONS` needs the cleaned payload first;
- a `TRANSFORMS-` rule earlier in the pipeline needs the cleaned text;
- SC4S caused the message-shape issue;
- you would be duplicating a vendor parser across every Splunk parsing tier.

## `INDEXED_EXTRACTIONS` versus `KV_MODE=json`

These are often confused.

### `INDEXED_EXTRACTIONS = JSON`

This is ingest-time structured parsing.

Fields are extracted while data is being processed for indexing.

Splunk explicitly warns that if you use:

```ini
INDEXED_EXTRACTIONS = JSON
```

you should not also configure:

```ini
KV_MODE = json
```

for the same source, or JSON fields can be extracted twice.

Reference: [Splunk structured-data field extraction](https://help.splunk.com/en/splunk-enterprise/get-started/get-data-in/9.3/configure-indexed-field-extraction/extract-fields-from-files-with-structured-data)

### `KV_MODE = json`

This is search-time automatic extraction.

The raw event is indexed and Splunk extracts JSON key/value fields when searching.

That changes where the troubleshooting should happen.

If `KV_MODE=json` is responsible and `_raw` becomes clean before indexing, the search-time parser can work.

If `INDEXED_EXTRACTIONS=json` is responsible, the raw structure has to be correct before that earlier structured parsing phase.

This is why I always inspect the effective sourcetype configuration instead of guessing.

On the HF:

```bash
/opt/splunk/bin/splunk \
  btool props list bitdefender:gz --debug
```

Then focus on:

```bash
/opt/splunk/bin/splunk \
  btool props list bitdefender:gz --debug \
| grep -Ei \
'INDEXED_EXTRACTIONS|KV_MODE|AUTO_KV_JSON|SEDCMD|TRANSFORMS-|REPORT-|EXTRACT-|LINE_BREAKER|SHOULD_LINEMERGE|TIME_'
```

## My preferred division of responsibility

After the Bitdefender issue, I settled on this rule.

### SC4S should handle

```text
transport-specific cleanup
syslog envelope parsing
source identification
vendor/product classification
index/sourcetype/host/source metadata
timestamp normalization where appropriate
removal of collector-side artifacts
preservation of the raw format expected by the TA
```

### Splunk TA/search tier should handle

```text
vendor domain fields
semantic extraction
FIELDALIAS
eventtypes
tags
CIM normalization
lookups
knowledge objects
```

In the Bitdefender case:

```text
SC4S:
[av] {"..."} -> {"..."}

Splunk TA:
JSON fields -> security semantics
```

I do not want to rebuild the Bitdefender TA inside SC4S.

I want SC4S to hand the TA the event shape it expects.

## Custom field extraction in SC4S

Sometimes no TA exists, or the source needs fields extracted before routing.

SC4S's current parser framework supports:

- `kv-parser`;
- `csv-parser`;
- `regexp-parser`;
- `json-parser`;
- `date-parser`;
- `syslog-parser`.

Reference: [SC4S parser methods](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/parse_message/)

### Key-value parser

For:

```text
src=10.0.0.1 action=deny user=alice
```

use:

```conf
parser {
    kv-parser(
        prefix(".values.")
        template("${MESSAGE}")
    );
};
```

### JSON parser

For a real JSON body:

```conf
parser {
    json-parser(
        prefix(".values.")
    );
};
```

### Regex parser

For irregular but stable syntax:

```conf
parser {
    regexp-parser(
        template("${MESSAGE}")
        patterns(
            '^device=(?<device>[^ ]+) action=(?<action>[^ ]+)'
        )
        prefix(".values.")
    );
};
```

Regex should be the tool I use because the format requires it, not because regex is familiar.

At high EPS, complicated PCRE paths are CPU workload.

## How extracted SC4S fields reach Splunk

SC4S supports two useful models.

### Model A — serialize extracted values into the event body

If I extract into:

```text
.values.*
```

I can use templates such as:

```text
t_kv_values
t_json_values
```

to serialize those fields into the event body.

### Model B — send indexed HEC fields

If I extract directly into:

```text
fields.*
```

SC4S includes those name/value pairs in the HEC payload as indexed fields.

Example:

```conf
parser {
    kv-parser(prefix("fields."));
};
```

This is powerful, but I use it intentionally.

Indexed fields increase index-time commitments. If the same field can be extracted cleanly at search time by a TA, I usually prefer the TA.

Reference: [SC4S extracted fields in Splunk](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/parse_message/)

## If I wanted to preserve the Bitdefender prefix as a field

Instead of simply deleting:

```text
[av]
[uc]
```

I could capture it first.

Conceptually:

```conf
parser {
    regexp-parser(
        template("${MESSAGE}")
        patterns(
            '^\[(?<bitdefender_channel>av|uc|hd|modules|antitampering|application-inventory)\]'
        )
        prefix("fields.")
    );
};

rewrite {
    subst(
        '^\[(av|uc|hd|modules|antitampering|application-inventory)\][[:space:]]*',
        "",
        value("MESSAGE")
    );
};
```

The final Splunk event could then be:

```text
_raw = {"event":"..."}
bitdefender_channel = av
```

I did not immediately do that because I did not want to invent semantics for those prefixes before validating what Bitdefender intends them to mean.

That is another operational rule:

> Do not turn an observed token into permanent indexed semantics until you know what it represents.

## SIMPLE is useful, but I do not treat it as the final parser for everything

SC4S SIMPLE let me onboard Bitdefender quickly:

```ini
SC4S_LISTEN_SIMPLE_BITDEFENDER_GZ_TCP_PORT=1514
```

with:

```csv
bitdefender_gz,index,av
bitdefender_gz,sourcetype,bitdefender:gz
bitdefender_gz,sc4s_template,t_msg_trim
```

That was useful.

But SC4S itself calls SIMPLE an interim path for well-formed RFC5424 or common RFC3164-style sources on a unique port.

If a source needs:

- deeper parsing;
- enrichment;
- nonstandard framing;
- source identification on shared ports;
- significant body normalization;
- complex field extraction;

I would move it to a dedicated SC4S log path.

Reference: [SC4S SIMPLE](https://splunk.github.io/splunk-connect-for-syslog/develop/sources/simple/)

## Raw first, parser second

The easiest way to waste time in syslog troubleshooting is to start writing regex before seeing the original event.

For each new source I now capture the wire data first:

```bash
tcpdump -ni any -s0 -A -c 10 \
  'host <SOURCE_IP> and tcp port <PORT>'
```

or:

```bash
tcpdump -ni any -s0 -A -c 10 \
  'host <SOURCE_IP> and udp port <PORT>'
```

I want to know:

```text
Is there PRI?
Is there an RFC timestamp?
Is there a hostname?
Is there a program?
Is the body JSON?
Is the body CEF?
Is there a prefix?
Are there embedded newlines?
Is the message actually RFC3164/5424?
```

SC4S also has raw-message troubleshooting features, but the documentation warns that storing RAWMSG doubles memory/disk requirements and should not be left enabled in production.

Reference: [SC4S obtain raw messages](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/troubleshooting/troubleshoot_resources.md)

## A troubleshooting method that follows the event instead of restarting services

This is the workflow I wish I had written down before the migration.

### Boundary 1 — Did the source send it?

Check the device:

```text
destination IP
port
protocol
facility/severity filters
source queue/drop counters
TLS state
```

### Boundary 2 — Did it reach the host?

```bash
tcpdump -ni any host <SOURCE_IP> and port <PORT>
```

If nothing arrives, do not troubleshoot HEC.

### Boundary 3 — Is anything listening?

UDP:

```bash
ss -lunp | grep ':5514'
```

TCP:

```bash
ss -lntp | grep ':1514'
```

If packets arrive in tcpdump but no process owns the socket, the problem is local listener configuration.

### Boundary 4 — Is SC4S healthy?

```bash
docker exec SC4S \
  syslog-ng-ctl healthcheck --timeout 5
```

A healthy result tells me the engine/main loop is alive.

It does **not** prove my source is being delivered.

### Boundary 5 — Is SC4S consuming the source?

```bash
docker exec SC4S syslog-ng-ctl stats
```

Take a baseline, generate a controlled event, then compare counters.

### Boundary 6 — What did SC4S think the event was?

In Splunk:

```spl
index=*
sc4s_fromhostip="<SOURCE_IP>"
| stats count by
    index
    sourcetype
    sc4s_vendor
    sc4s_product
    sc4s_proto
    sc4s_destport
```

If the event lands in fallback, I investigate source identification before changing Splunk TA settings.

### Boundary 7 — What did SC4S try to send to HEC?

Look at SC4S logs:

```bash
docker logs --since 10m SC4S 2>&1 \
| grep -Ei \
  'status_code|Incorrect index|HEC|error|drop|queue'
```

A useful error often includes the actual HEC request:

```json
{
  "index":"netfw",
  "sourcetype":"fortigate_traffic"
}
```

That is better evidence than guessing what the metadata *should* have been.

### Boundary 8 — Is the HEC endpoint itself healthy?

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

### Boundary 9 — Can the token write to the exact target index?

```bash
read -rsp "HEC token: " HEC_TOKEN
echo

curl --fail-with-body \
  --cacert /opt/sc4s/tls/trusted.pem \
  -H "Authorization: Splunk ${HEC_TOKEN}" \
  -H "Content-Type: application/json" \
  https://splunk-hec.example.net:8088/services/collector/event \
  -d '{
    "index":"fgt",
    "sourcetype":"sc4s:manual:test",
    "event":"SC4S-HANDOFF-TEST"
  }'

unset HEC_TOKEN
```

### Boundary 10 — What parsing config is effective on the HF?

```bash
/opt/splunk/bin/splunk \
  btool props list <sourcetype> --debug
```

Then inspect:

```text
INDEXED_EXTRACTIONS
LINE_BREAKER
TIME_*
TRANSFORMS-
SEDCMD
KV_MODE
REPORT-
EXTRACT-
```

### Boundary 11 — What is actually indexed?

```spl
index=<target>
| table
    _time
    _indextime
    index
    host
    source
    sourcetype
    _raw
```

Only after these boundaries do I restart arbitrary components.

## HEC 400 versus a transient outage

This changed how I think about buffering.

A connection failure, timeout, or service-unavailable response can be transient.

HTTP 400 means the request itself is invalid.

For example:

```json
{"text":"Incorrect index","code":7}
```

SC4S can treat that as non-retryable.

That means disk buffering cannot save me from a configuration error that causes Splunk to reject the request permanently.

This is why HEC index governance is part of data-loss prevention.

My onboarding order is now:

```text
1. create index
2. authorize index on HEC token
3. manually test HEC to that index
4. verify SC4S metadata key
5. add override if required
6. restart SC4S
7. send synthetic event
8. observe dropped counter
9. enable production source
```

## Why one bad HEC index can hurt unrelated events

SC4S batches HEC events.

In one failure I saw:

```text
batch_size='7'
invalid-event-number=3
```

The log entry displayed an `sc4s:events` payload targeting `main`, which made it look as if `main` was invalid.

But the response was identifying one invalid member of a mixed batch.

One event targeting an unauthorized index can cause the batch to be rejected.

This is why I watch the destination counters:

```bash
docker exec SC4S \
  syslog-ng-ctl stats \
  | grep 'dst.http;d_hec_fmt'
```

Healthy steady state:

```text
written -> increasing
queued  -> normally low/zero
dropped -> not increasing
```

I care more about the **delta** than the historical total.

A historical `dropped=36995` is evidence of an earlier incident. If it stays at 36995 while `written` rises, the current path can still be healthy.

## Disk buffering: what it protects and what it does not

SC4S disk buffering protects this segment:

```text
SC4S -> HEC destination
```

It does not protect:

```text
UDP source -> SC4S
```

if the UDP packet never reaches or is consumed by SC4S.

That is why I never say:

> "I enabled disk buffering, therefore UDP is reliable."

Those are different boundaries.

SC4S documents an approximate disk-buffer sizing formula:

```text
peak EPS
× average event bytes
× outage seconds
× ~1.7 overhead
```

Example:

```text
20,000 EPS
× 800 bytes
× 14,400 seconds
× 1.7
≈ 391.7 GB
```

I would provision more than that.

The buffer also needs a way to drain.

If normal traffic is:

```text
40k EPS
```

and the recovered HEC path can deliver only:

```text
35k EPS
```

the queue cannot shrink.

A buffer buys time. It does not create throughput.

Reference: [SC4S disk buffering](https://splunk.github.io/splunk-connect-for-syslog/latest/configuration/)

## How I test buffering instead of trusting the setting

A real acceptance test should:

1. record destination counters;
2. make all HEC destinations unavailable;
3. generate numbered **TCP** test events;
4. confirm queue/disk growth;
5. restart SC4S while HEC is still unavailable;
6. restore HEC;
7. confirm the queue drains;
8. count events in Splunk;
9. look for duplicates;
10. verify `dropped` did not increase unexpectedly.

Why use TCP-generated test events for the buffer test?

Because I want to test:

```text
SC4S -> HEC durability
```

without mixing in:

```text
source -> SC4S UDP loss
```

A good test isolates one behavior.

## High-load traffic: do not tune the component you can see and ignore the queues you cannot

A high-EPS path is a chain of finite queues:

```text
source queue
   |
NIC ring
   |
kernel socket buffer
   |
SC4S receive socket
   |
SC4S input window
   |
parser CPU
   |
HEC worker/batch queue
   |
disk buffer
   |
network
   |
HEC receiver
   |
Splunk parsing queues
   |
indexing queues
   |
storage
```

When events drop, the first question is not:

> "Which SC4S tuning variable should I increase?"

It is:

> "Which queue filled first?"

## Linux receive buffers

Current SC4S runtime guidance recommends matching Linux receive buffers to SC4S's default UDP buffer.

A documented baseline is:

```ini
net.core.rmem_default = 17039360
net.core.rmem_max = 17039360
```

Apply according to your OS configuration process.

Then monitor:

```bash
netstat -su | grep -i 'receive errors'
```

If the kernel receive error count rises during bursts, packets are being lost before SC4S can process them.

Reference: [SC4S runtime configuration](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/gettingstarted/getting-started-runtime-configuration.md)

## Large receive buffers: useful, not magical

SC4S tuning documentation gives examples with much larger socket buffers for heavy traffic.

Large buffers can absorb bursts.

They also:

- consume memory;
- increase queued latency;
- hide a sustained throughput deficit for longer.

So:

```text
buffer size != sustainable EPS
```

A large queue can make a system look healthy for ten minutes before it fails more dramatically.

Benchmark the steady state.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## UDP input windows and fetch limits

SC4S exposes UDP input-window and fetch-limit tuning.

Examples:

```ini
SC4S_SOURCE_UDP_IW_USE=yes
SC4S_SOURCE_UDP_IW_SIZE=1000000
```

and:

```ini
SC4S_SOURCE_UDP_FETCH_LIMIT=1000
```

The input window can absorb temporary downstream slowdown.

The fetch limit controls how many messages are pulled in a read cycle.

Too small can waste CPU on loop overhead.

Too large can let one source dominate processing.

I tune them together and test with the real message mix.

## Multiple UDP sockets and eBPF

SC4S can open multiple UDP sockets:

```ini
SC4S_SOURCE_LISTEN_UDP_SOCKETS=32
```

With many independent senders, Linux hashing can distribute flows across sockets.

A single huge sender is different. One flow can keep landing on the same socket/worker.

SC4S provides eBPF support to improve distribution in that situation:

```ini
SC4S_ENABLE_EBPF=yes
SC4S_EBPF_NO_SOCKETS=32
```

This can increase parallelism significantly in the right workload.

Trade-offs include:

- privileged runtime requirements;
- more operational complexity;
- possible reordering considerations;
- another kernel-level dependency.

I would enable it only after proving one heavy UDP flow is the bottleneck.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## TCP parallelization

A single high-volume TCP connection can serialize a lot of work.

SC4S supports:

```ini
SC4S_ENABLE_PARALLELIZE=yes
SC4S_PARALLELIZE_NO_PARTITION=4
```

This is useful when one connection dominates.

If I already have many independent TCP connections, adding parallelization can add overhead without solving a real problem.

Same rule:

> Measure first.

## Parser cost can be the bottleneck

SC4S source identification and parsing consume CPU.

Regex is particularly worth watching.

If every event has to pass a large parser catalog and several expensive patterns, the system can become CPU-bound even when the NIC and HEC destination are fine.

Current SC4S tuning guidance suggests considering SC4S Lite when the source set is well known, because reducing parser evaluation can improve real-world capacity.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## HEC workers and downstream capacity

SC4S HEC destinations have worker controls.

Changing worker count can help at extreme volume, but it is not the first tuning knob I reach for.

HEC throughput depends on:

- event size;
- TLS;
- latency;
- batch size;
- Splunk receiver capacity;
- indexer storage;
- destination count;
- disk-buffer state.

If the indexers are saturated, increasing SC4S workers can simply apply pressure faster.

## When I would create another SC4S instance

Not because I created another index.

I would create another SC4S service/host when I want another failure or capacity domain.

Examples:

- one firewall produces most of the EPS;
- DMZ and internal sources should not share a collector;
- separate sites need edge collection;
- a very expensive parser dominates CPU;
- maintenance ownership is different;
- geography or compliance requires separation.

SC4S tuning guidance specifically suggests a dedicated instance when one log source produces a large percentage of total traffic.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## Why I do not put a normal load balancer in front of UDP syslog and call it HA

SC4S documentation is deliberately cautious about conventional front-side syslog load balancing.

Reasons include:

- UDP has no session state;
- source IP can be obscured;
- TCP connections may be long-lived and distribute unevenly;
- hashing can make one collector hot;
- another network appliance becomes another drop point.

SC4S recommends edge collection and vertical scaling before conventional horizontal load balancing for syslog.

It also discusses specific HA patterns, including more advanced network approaches.

The bigger point is:

> Do not apply HTTP architecture assumptions to syslog just because both cross a network.

Reference: [SC4S architecture](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)

## Where macvlan fits — and where it does not

I consider macvlan an infrastructure tool, not an SC4S performance feature.

Docker macvlan can give the SC4S container its own:

```text
MAC address
IP address
L2 identity
```

This can help when:

- legacy devices expect a collector at a dedicated IP;
- I want to separate the collector IP from the Docker host;
- host-network port conflicts are undesirable;
- network controls are based on a dedicated L2/L3 identity;
- I want migration compatibility with an old collector address.

Example:

```bash
docker network create -d macvlan \
  --subnet=192.0.2.0/24 \
  --gateway=192.0.2.1 \
  -o parent=ens192 \
  sc4s_l2
```

Then:

```bash
docker run \
  --network sc4s_l2 \
  --ip 192.0.2.50 \
  ...
```

But macvlan does not fix:

```text
regex CPU
small receive buffers
HEC 400 errors
slow indexers
disk saturation
one overloaded TCP stream
```

Docker also documents real constraints:

- Linux-only;
- many cloud providers block it;
- network equipment must tolerate multiple MACs;
- too many MACs can cause VLAN spread;
- macvlan containers cannot communicate directly with the host by default because of a Linux kernel restriction.

If multiple MACs are a problem, ipvlan can be worth evaluating.

Reference: [Docker macvlan](https://docs.docker.com/engine/network/drivers/macvlan/)

## Host networking versus macvlan

The SC4S systemd container pattern often uses:

```text
--network host
```

That is simple.

Benefits:

- no port publishing;
- source networking is easy to inspect;
- fewer NAT layers;
- many syslog ports can be used naturally.

Costs:

- SC4S ports are host ports;
- port conflicts are direct;
- container network isolation is lower.

Macvlan gives a dedicated network identity but introduces L2 complexity.

I use host networking unless there is a concrete network-architecture reason not to.

Reference: [Docker host network](https://docs.docker.com/engine/network/drivers/host/)

## The SC4S status port 8080

SC4S runs an HTTP status/health service, normally on port 8080.

In host networking mode that can become reachable on the host network.

If I only need local monitoring, current SC4S supports binding the status service to localhost:

```ini
SC4S_LISTEN_STATUS_HOST=127.0.0.1
```

That is cleaner than exposing plain HTTP to the network and hoping perimeter rules remain correct.

Reference: [SC4S configuration](https://splunk.github.io/splunk-connect-for-syslog/latest/configuration/)

## Security hardening decisions

A logging collector becomes a security-sensitive system quickly because it receives data from many trusted infrastructure devices and holds credentials for the next hop.

My baseline:

### Network

Allow only required source-to-listener flows.

```text
FortiGate -> UDP/TCP listener
Bitdefender -> TCP/1514
SC4S -> HEC/8088
admin network -> SSH
monitoring -> status endpoint only if needed
```

### HEC

Use a dedicated SC4S token.

Do not reuse administrator credentials.

Rotate exposed tokens.

### TLS

Use certificate verification in production.

Do not permanently hide trust problems with:

```ini
TLS_VERIFY=no
```

### Files

```bash
chown root:root /opt/sc4s/env_file
chmod 0600 /opt/sc4s/env_file
```

### Container image

Pin an approved release/digest.

For an air gap, keep the approved image locally and retain the previous image for rollback.

### Configuration

Store sanitized config in Git.

Never commit real tokens.

## Archive and disk buffer are not the same thing

SC4S can archive events locally.

That is different from the disk buffer.

### Disk buffer

Purpose:

```text
temporary delivery resilience
```

### Archive

Purpose:

```text
intentional local retention
```

If I enable archive, I need:

- retention;
- rotation;
- capacity monitoring;
- access control;
- recovery procedures.

Do not call a transient queue an archive.

## How I would structure SC4S configuration in Git

Something like:

```text
sc4s/
  README.md
  env_file.example

  context/
    splunk_metadata.csv
    compliance_meta_by_source.conf
    compliance_meta_by_source.csv

  config/
    app_parsers/
      rewriters/
        app-bitdefender-strip-prefix.conf

  systemd/
    sc4s.service.offline.example

  tests/
    fortigate-sample.txt
    bitdefender-av-sample.txt
    bitdefender-license-sample.txt
    expected-results.md
```

Every source change should include:

```text
vendor/model
firmware/version
transport
sample raw event
target index
target sourcetype
expected host
expected event time
expected _raw
expected important fields
HEC authorization
rollback
```

That turns "syslog config" into reviewable engineering work.

## How I test a new source before production

I now use this sequence.

### Step 1 — Capture raw

```bash
tcpdump ...
```

### Step 2 — Identify format

Is it:

```text
RFC3164
RFC5424
JSON-in-syslog
CEF
raw JSON
vendor-specific
```

### Step 3 — Check SC4S support

Search the SC4S source documentation and local metadata example.

### Step 4 — Decide whether I need

```text
built-in parser
SIMPLE
dedicated custom log path
```

### Step 5 — Decide metadata

```text
index
sourcetype
host
source
template
```

### Step 6 — Create/authorize the Splunk index

### Step 7 — Test HEC manually

### Step 8 — Enable source

### Step 9 — Validate `_raw`

### Step 10 — Validate TA fields

### Step 11 — Check `dropped` delta

### Step 12 — Run an outage test

That process is slower than blindly opening a port.

It is much faster than finding silent parsing damage three weeks later.

## A practical baseline `env_file`

A sanitized version of the configuration pattern I ended up with looks like:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_URL=https://splunk-hec.example.net:8088
SC4S_DEST_SPLUNK_HEC_DEFAULT_TOKEN=<SECRET>
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=yes

SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_ENABLE=yes
SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_RELIABLE=no

# FortiGate migration input
SC4S_LISTEN_DEFAULT_UDP_PORT=5514
SC4S_OPTION_FORTINET_SOURCETYPE_PREFIX=fortigate

# Bitdefender SIMPLE input
SC4S_LISTEN_SIMPLE_BITDEFENDER_GZ_TCP_PORT=1514

# Keep local unless remote monitoring requires exposure
SC4S_LISTEN_STATUS_HOST=127.0.0.1

# Future FortiGate reliable syslog after validation
#SC4S_LISTEN_DEFAULT_RFC6587_PORT=601
```

No copied global timezone.

No undocumented Bitdefender sourcetype-prefix option.

No real token in source control.

## The metadata override file from this deployment

Sanitized:

```csv
bitdefender_gz,index,av
bitdefender_gz,sourcetype,bitdefender:gz
bitdefender_gz,sc4s_template,t_msg_trim

fortinet_fortios_traffic,index,fgt
fortinet_fortios_utm,index,fgt
fortinet_fortios_event,index,fgt
fortinet_fortios_log,index,fgt
```

Why override FortiGate?

Because my index governance uses `fgt`, while SC4S's recommended defaults use its own taxonomy.

Why override Bitdefender sourcetype/template?

Because this was a SIMPLE source I defined, and the downstream Splunk TA expected `bitdefender:gz` with a JSON-shaped body.

That is exactly the kind of place where metadata customization is justified.

## The HEC input pattern

Sanitized:

```ini
[http]
disabled = 0
port = 8088
enableSSL = 1

[http://sc4s]
disabled = 0
token = <SECRET>
description = SC4S ingestion
index = main
indexes = main,fgt,av
useACK = 0
```

The important mental model:

```text
index=main
```

is the default.

```text
indexes=main,fgt,av
```

is the authorization set.

SC4S still chooses `fgt` or `av` per event.

When I add `dlp`, I either:

- add `dlp` to the restricted token first; or
- deliberately use a broader token policy.

I do not let a new source discover the authorization mistake in production traffic.

## HEC index acknowledgement

SC4S has historically documented that its syslog-ng HTTP destination does not support Splunk HEC indexer acknowledgement semantics in the way a Splunk forwarder does.

So I do not turn on:

```ini
useACK = 1
```

just because "ACK sounds more reliable."

Reliability mechanisms have to be supported end-to-end.

For SC4S I rely on:

```text
HTTP result handling
multiple HEC endpoints / load balancing
persistent disk buffering
outage tests
monitoring
```

and I keep an eye on current SC4S release documentation in case this support position changes.

## Why the Heavy Forwarder still needs the right TA

Because the HF parses before forwarding cooked events, ingest-time TA configuration belongs there in this topology.

If the Bitdefender TA includes:

```text
INDEXED_EXTRACTIONS
LINE_BREAKER
TIME_*
TRANSFORMS
SEDCMD
```

those settings must be available on the HF that receives HEC.

Search-time knowledge such as:

```text
KV_MODE
REPORT-
EXTRACT-
FIELDALIAS
EVAL-
LOOKUP-
eventtypes/tags
```

belongs on the search tier according to normal Splunk app deployment practice.

Do not install a TA only on the search head when it contains ingest-time parsing rules that the HF needs.

Reference: [Splunk intermediate HF implementation considerations](https://help.splunk.com/en/splunk-enterprise/splunk-validated-architectures/getting-data-in-forwarding-and-preprocessing/intermediate-data-routing-using-universal-and-heavy-forwarders)

## How I troubleshoot field extraction specifically

When a field disappears after inserting SC4S, I do not start with `props.conf`.

I compare the event at each stage.

### What the source sent

```bash
tcpdump -A
```

### What Splunk indexed as `_raw`

```spl
index=<index> sourcetype=<st>
| head 5
| table _raw
```

### Is it valid JSON?

```spl
index=<index> sourcetype=<st>
| head 20
| spath
| fieldsummary
```

### What does the TA expect?

```bash
splunk btool props list <sourcetype> --debug
```

### Does `_raw` start with the expected character?

For JSON:

```spl
index=<index> sourcetype=<st>
| eval first_char=substr(trim(_raw),1,1)
| stats count by first_char
```

### Did SC4S add or preserve a prefix?

```spl
index=av sourcetype="bitdefender:gz"
| rex field=_raw "^(?<prefix>\[[^]]+\])"
| stats count by prefix
```

This is how the `[av]`, `[uc]`, `[hd]`, `[modules]`, `[antitampering]`, and `[application-inventory]` populations became visible.

## Synthetic tests are better than waiting for the next real incident

For the Bitdefender post-filter I can send:

```bash
printf '<134>1 2026-09-02T16:30:00Z bd-test gravityzone - - - [av] {"test":"SC4S-BD-AV-001"}\n' \
| nc -N 127.0.0.1 1514
```

and:

```bash
printf '<134>1 2026-09-02T16:30:01Z bd-test gravityzone - - - [uc] {"test":"SC4S-BD-UC-001"}\n' \
| nc -N 127.0.0.1 1514
```

plus an already-clean JSON event:

```bash
printf '<134>1 2026-09-02T16:30:02Z bd-test gravityzone - - - {"test":"SC4S-BD-CLEAN-001"}\n' \
| nc -N 127.0.0.1 1514
```

Then:

```spl
index=av sourcetype="bitdefender:gz"
(
  "SC4S-BD-AV-001"
  OR "SC4S-BD-UC-001"
  OR "SC4S-BD-CLEAN-001"
)
| table _time _raw sc4s_destport sc4s_proto
```

All should begin with `{` after the rewrite/template chain.

That gives me a repeatable regression test.

## Performance testing should include correctness

SC4S publishes performance guidance and uses tools such as `loggen`.

A test such as:

```bash
loggen \
  --interval 60 \
  --rate 27000 \
  -s 1000 \
  --no-framing \
  --dgram \
  <SC4S_IP> 514
```

is useful.

But a performance result is incomplete if I report only:

```text
27k EPS
```

I also want:

```text
events sent
events received
events written to HEC
events indexed
kernel receive errors
SC4S dropped delta
queue depth
ingestion latency
CPU
memory
disk latency
```

Fast loss is not high performance.

Reference: [SC4S performance tests](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/performance-tests/)

## Edge collection is one of the strongest reliability improvements

The more I worked through this, the more I agreed with SC4S's edge-collection recommendation.

For UDP especially:

```text
source -> local/nearby collector -> reliable HTTP path -> Splunk
```

is easier to reason about than:

```text
source -> WAN -> load balancer -> central syslog -> Splunk
```

Every extra stateless network hop is another place to lose a packet with little evidence.

When possible, put the collector close to the high-value/high-volume sources.

Reference: [SC4S architecture](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)

## Best practices that came out of the migration

These are not theoretical rules. Each one maps to a failure or near-failure I actually hit.

### Separate collection from downstream availability

Do not make a restart of your Splunk parsing process equal a blind spot at the network edge.

### Keep rollback during migration

Stop/mask an old daemon before deleting it.

### Test HEC before SC4S

A collector should not be your HEC troubleshooting tool.

### Test the exact target index

`HEC healthy` does not mean `HEC authorized for fgt`.

### Treat SC4S defaults as defaults

`netfw` is useful, not mandatory.

### Treat sourcetypes as contracts

Changing them can break TAs.

### Preserve raw format expected by the TA

If the TA expects JSON, hand it JSON.

### Do not use `SEDCMD` without understanding pipeline order

Earlier phases may already have needed the unmodified body.

### Watch deltas

Historical `dropped` counts can remain nonzero after recovery.

### Buffering is not source reliability

It starts after SC4S has received the event.

### Tune bottlenecks, not knobs

Measure socket drops, CPU, disk, HEC, and Splunk queues separately.

### Do not copy unexplained environment variables

That is how an unrelated timezone becomes production behavior.

## What I would change in the next iteration

The current setup solved the immediate reliability and onboarding problem, but I would still improve it.

### Move FortiGate from UDP to validated reliable TCP/RFC6587

Only after confirming the FortiOS version and framing behavior.

### Remove the unnecessary intermediate HF if architecture permits

Preferred target:

```text
SC4S -> HEC VIP/indexers
```

If the HF remains, document exactly what function justifies it.

### Turn every parser workaround into a regression test

Especially the Bitdefender prefix rewrite.

### Measure real peak EPS and average event size

Use those numbers for buffer and host sizing instead of generic estimates.

### Decide whether Bitdefender deserves a dedicated SC4S log path

SIMPLE worked as an onboarding bridge. If the source requires more normalization, a dedicated parser is cleaner.

### Build alerting around data freshness, not only process health

A green `systemctl status` does not prove a firewall is still indexing events.

## A small operational dashboard I would build

For each critical source:

```spl
index IN (fgt,av,dlp,cisco)
| stats
    count
    latest(_time) AS last_event
    latest(_indextime) AS last_index
    by index sourcetype host
| eval
    event_age=now()-last_event,
    index_age=now()-last_index
| convert
    ctime(last_event)
    ctime(last_index)
| sort - event_age
```

And monitor SC4S internal events separately:

```spl
index=main sourcetype="sc4s:events"
| sort - _time
```

I also want alerts for:

```text
dropped counter increasing
buffer growth
disk threshold
UDP receive errors
HEC HTTP 4xx/5xx
source freshness gap
SC4S service restart
```

## Final architecture view

The architecture I now reason about is not "a syslog server."

It is:

```text
                          SOURCE LAYER
     +-------------------------------------------------+
     | FortiGate | Bitdefender | Cisco | DLP | others |
     +--------------------+----------------------------+
                          |
                    UDP / TCP / TLS
                          |
                          v

                         SC4S
     +-------------------------------------------------+
     | socket / framing                                |
     | syslog envelope                                 |
     | source identification                           |
     | parser                                          |
     | post-filter / rewrite                           |
     | metadata: index / sourcetype / host / source    |
     | output template                                 |
     | HEC batching                                    |
     | persistent disk buffer                          |
     +--------------------+----------------------------+
                          |
                     HTTPS / HEC
                          |
                          v

                SPLUNK PARSING TIER
     +-------------------------------------------------+
     | HEC input                                       |
     | INDEXED_EXTRACTIONS                             |
     | line/time parsing                               |
     | TRANSFORMS                                      |
     | SEDCMD                                          |
     | parsed/cooked forwarding if HF                  |
     +--------------------+----------------------------+
                          |
                          v

                     INDEXER TIER
     +-------------------------------------------------+
     | raw data + index files                          |
     +--------------------+----------------------------+
                          |
                          v

                      SEARCH TIER
     +-------------------------------------------------+
     | KV_MODE / REPORT / EXTRACT                      |
     | FIELDALIAS / EVAL / LOOKUP                      |
     | eventtypes / tags / CIM                         |
     +-------------------------------------------------+
```

When something breaks, I ask which box changed.

That question has been more useful than any individual SC4S setting.

## Command cheat sheet

### SC4S service

```bash
systemctl status sc4s --no-pager -l
systemctl restart sc4s
journalctl -u sc4s --since "10 minutes ago" --no-pager
```

### Container

```bash
docker ps --filter name=SC4S
docker logs --tail 200 SC4S
docker inspect SC4S
```

### Health

```bash
docker exec SC4S \
  syslog-ng-ctl healthcheck --timeout 5
```

### Destination statistics

```bash
docker exec SC4S \
  syslog-ng-ctl stats \
| grep 'dst.http;d_hec_fmt'
```

### Effective generated config

```bash
docker exec SC4S \
  syslog-ng-ctl config --preprocessed
```

### Listener state

```bash
ss -lunp
ss -lntp
```

### UDP kernel state

```bash
netstat -su
```

### Packet capture

```bash
tcpdump -ni any -s0 -A \
  'host <SOURCE_IP> and port <PORT>'
```

### HEC health

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

### Splunk effective sourcetype parsing

```bash
/opt/splunk/bin/splunk \
  btool props list <sourcetype> --debug
```

### SC4S source classification

```spl
index=* sc4s_fromhostip="<SOURCE_IP>"
| stats count by
    index
    sourcetype
    sc4s_vendor
    sc4s_product
    sc4s_proto
    sc4s_destport
```

### Ingestion delay

```spl
index=<index>
| eval ingest_delay=_indextime-_time
| stats
    count
    avg(ingest_delay)
    max(ingest_delay)
    by sourcetype
```

## Reference map

### SC4S core

- [SC4S project repository](https://github.com/splunk/splunk-connect-for-syslog)
- [SC4S documentation](https://splunk.github.io/splunk-connect-for-syslog/main/)
- [Architecture considerations](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)
- [Quickstart](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/quickstart_guide/)
- [Splunk setup for SC4S](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-splunk-setup/)
- [Runtime configuration](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-runtime-configuration/)
- [Configuration and metadata overrides](https://splunk.github.io/splunk-connect-for-syslog/latest/configuration/)
- [Destinations](https://splunk.github.io/splunk-connect-for-syslog/main/destinations/)
- [SIMPLE source](https://splunk.github.io/splunk-connect-for-syslog/develop/sources/simple/)
- [Fortinet FortiOS source](https://splunk.github.io/splunk-connect-for-syslog/main/sources/vendor/Fortinet/fortios/)
- [Parser development](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/)
- [Parser methods / extracted fields](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/parse_message/)
- [Filter development](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/filter_message/)
- [Troubleshooting / raw messages / post-filters](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/troubleshooting/troubleshoot_resources.md)
- [Fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)
- [Performance tests](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/performance-tests/)
- [SC4S releases](https://github.com/splunk/splunk-connect-for-syslog/releases)

### Splunk pipeline

- [How data moves through Splunk deployments](https://help.splunk.com/en/splunk-enterprise/administer/distributed-deployment-manual/10.4/overview-of-splunk-enterprise-distributed-deployments/how-data-moves-through-splunk-deployments-the-data-pipeline)
- [Configuration parameters and the data pipeline](https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/10.2/administer-splunk-enterprise-with-configuration-files/configuration-parameters-and-the-data-pipeline)
- [props.conf reference](https://help.splunk.com/en/splunk-enterprise/administer/admin-manual/10.4/configuration-file-reference/10.4.0-configuration-file-reference/props.conf)
- [Structured-data indexed extraction](https://help.splunk.com/en/splunk-enterprise/get-started/get-data-in/9.3/configure-indexed-field-extraction/extract-fields-from-files-with-structured-data)
- [Heavy and Universal Forwarder types](https://help.splunk.com/en/data-management/forward-data/forwarding-and-receiving-data/10.4.2604/introduction-to-forwarding/types-of-forwarders)
- [Intermediate Heavy Forwarder architecture](https://help.splunk.com/en/splunk-enterprise/splunk-validated-architectures/getting-data-in-forwarding-and-preprocessing/intermediate-data-routing-using-universal-and-heavy-forwarders)
- [metrics.log](https://help.splunk.com/data-management/monitor-and-troubleshoot/troubleshoot-splunk-enterprise/9.1/splunk-enterprise-log-files/about-metrics.log)

### Syslog engine

- [AxoSyslog documentation](https://axoflow.com/docs/axosyslog-core/)
- [Templates/macros](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/customizing-message-format/configuring-macros/)
- [Rewrite rules](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/modifying-messages/)
- [Regex parser](https://axoflow.com/docs/axosyslog-core/chapter-parsers/parser-regexp/)

### Container networking

- [Docker host networking](https://docs.docker.com/engine/network/drivers/host/)
- [Docker macvlan](https://docs.docker.com/engine/network/drivers/macvlan/)
- [Docker network drivers](https://docs.docker.com/engine/network/drivers/)

---

## Closing note

The most useful change I made was not replacing the Heavy Forwarder with SC4S.

It was changing how I debug the path.

Before this migration, when logs disappeared, the question was too broad:

> Why is Splunk not receiving logs?

Now I ask:

```text
Did the device send it?
Did the packet reach Linux?
Did the socket receive it?
Did SC4S identify it?
What metadata did SC4S assign?
What did the HEC request contain?
Did Splunk authorize that index?
What did the HF do during structured/parsing phases?
What was finally indexed as _raw?
What did the TA extract at search time?
```

That turns a vague logging problem into a series of testable boundaries.

SC4S did not remove the complexity of syslog. It made the complexity visible enough to manage.
