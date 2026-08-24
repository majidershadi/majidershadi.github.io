# SC4S in Production: From Syslog Fundamentals to High-Load Splunk Ingestion

**A hands-on architecture, deployment, tuning, customization, and troubleshooting guide for Splunk Connect for Syslog**

Published: August 24, 2026

Last technically verified: August 24, 2026

Author: Majid Ershadi

> **Scope and version note.** This guide is written against the SC4S 3.x architecture and was technically checked against current SC4S documentation in August 2026. The examples intentionally separate stable design principles from version-sensitive environment variables. SC4S evolves quickly: check the release notes and the `splunk_metadata.csv.example` file shipped with the exact image you deploy before treating any key, parser, or tuning value as permanent.

I wrote this after troubleshooting a mixed FortiGate and Bitdefender path through SC4S and Splunk HEC. The collector started cleanly; the difficult failures appeared later in the path: HEC index authorization, metadata overrides, and a JSON body that reached Splunk in the wrong raw shape. That experience determines the order used here—inspect each boundary, preserve evidence, and tune only after the data path is understood.

## 1. Why this guide exists

Syslog looks simple until it becomes important.

At small scale the architecture is often:

```text
device -> UDP/514 -> something that listens -> Splunk
```

That can work for months. Then the first real outage, restart, traffic burst, malformed vendor message, certificate change, or index-routing mistake exposes everything that the simple diagram left out.

A production syslog layer has to answer more questions:

- What happens when Splunk is unavailable for an hour?
- What happens when a firewall produces a sudden 10x burst?
- Where is the first point at which an event can be lost?
- Does a TCP connection actually make the complete path reliable?
- Which system decides `index`, `sourcetype`, `host`, and `source`?
- What happens when a new SC4S parser chooses an index that the HEC token is not allowed to use?
- How do you preserve raw JSON when a vendor wraps JSON inside a syslog envelope?
- How do you onboard an unsupported product without turning the collector into a pile of ad-hoc regexes?
- How do you prove that an event was received, classified, buffered, sent, and indexed?
- How do you scale without putting a generic load balancer in front of a protocol that was never designed for modern load balancing?

That gap is where I have found SC4S useful. I treat it as an ingestion layer rather than just a syslog daemon in a container. It brings together the syslog engine, vendor identification, Splunk metadata assignment, timestamp handling, HEC output, persistent buffering, health instrumentation, and a maintained catalog of source-specific parsing behavior.

The project describes itself as an open-source packaged solution built on syslog-ng/AxoSyslog and Splunk HEC. Its purpose is to reduce inconsistent syslog deployments, catch-all `syslog` sourcetypes, deep syslog expertise requirements, and uneven distribution into Splunk.

The project documentation is the starting point for the details in this guide:

- [SC4S project](https://github.com/splunk/splunk-connect-for-syslog)
- [SC4S documentation](https://splunk.github.io/splunk-connect-for-syslog/main/)
- [SC4S architecture considerations](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)

## 2. First principles: syslog is a transport family, not a data model

Before discussing SC4S, separate four concepts that are frequently mixed together:

1. **Transport** — UDP, TCP, TLS.
2. **Framing** — how one event is separated from the next on a byte stream.
3. **Message format** — RFC3164-like BSD syslog, RFC5424, CEF, LEEF, JSON-in-syslog, vendor-specific text, and noncompliant variants.
4. **Splunk metadata** — index, sourcetype, host, source, event time, and indexed fields.

SC4S sits between these worlds.

A FortiGate message can arrive over UDP and still contain a vendor timestamp and timezone. A Bitdefender message can arrive over TCP but contain a JSON body. A Cisco device can use a normal syslog port while requiring a completely different parser and Splunk sourcetype.

The receiving port does not define the index. TCP does not define the sourcetype. HEC does not automatically know the vendor.

That separation is the core mental model for operating SC4S correctly.

## 3. What SC4S actually does in the pipeline

A simplified SC4S path is:

```text
network packet / TCP stream
        |
        v
Linux kernel receive buffers
        |
        v
SC4S listener (syslog-ng/AxoSyslog)
        |
        v
syslog parsing and source identification
        |
        v
vendor/product log path
        |
        +--> timestamp handling
        +--> message normalization
        +--> metadata assignment
        |      index
        |      sourcetype
        |      source
        |      host
        |      template
        |
        v
HEC formatting and batching
        |
        v
memory / persistent disk buffering
        |
        v
HTTPS / HEC
        |
        v
Splunk indexer HEC endpoint
        |
        v
Splunk parsing/indexing/search
```

Troubleshooting becomes much easier when you stop asking "why are my logs missing?" and instead ask which stage failed.

## 4. Where SC4S belongs in a Splunk architecture

For Splunk Enterprise, the preferred path is:

```text
log sources
    |
    v
SC4S
    |
    | HTTPS / HEC
    v
HEC endpoint(s) on indexers
    |
    v
indexer cluster
```

SC4S documentation explicitly recommends sending SC4S HEC traffic directly to indexer HEC endpoints instead of placing a Heavy Forwarder in the middle.

A Heavy Forwarder can still be technically useful when it performs a real function that you cannot place elsewhere, for example:

- mandatory enterprise routing;
- masking or transformation that must happen on the Splunk tier;
- network segmentation that prevents SC4S from reaching indexers;
- a temporary migration dependency.

But a Heavy Forwarder used only as:

```text
SC4S -> HEC -> HF -> S2S -> indexers
```

adds another queue, certificate, process, restart domain, and potential bottleneck without improving source-side reliability.

Reference: [SC4S Splunk setup](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-splunk-setup/)

## 5. SC4S versus the common alternatives

### Heavy Forwarder as a syslog receiver

Advantages:

- familiar Splunk administration;
- native forwarding onward to indexers;
- can perform Splunk parsing/routing functions.

Weaknesses:

- it is a large Splunk process for a job that a dedicated syslog engine performs more naturally;
- source-side UDP events are still volatile;
- service restart or blocked queues can create operational blind spots;
- vendor identification frequently ends up as local `props.conf`/`transforms.conf` engineering;
- buffering and raw syslog lifecycle are less explicit than a dedicated collection tier.

Use an HF when you need HF capabilities. Do not make it your default syslog daemon simply because it already exists.

### Plain syslog-ng or rsyslog

Advantages:

- mature;
- fast;
- highly flexible;
- excellent for non-Splunk destinations;
- complete control over parsing, files, queues, and network transport.

Weakness in Splunk-heavy environments:

You own the integration contract yourself.

You must decide and maintain:

- vendor recognition;
- `index`;
- `sourcetype`;
- timestamp behavior;
- HEC payload construction;
- batching;
- retries;
- Splunk-specific metadata;
- compatibility with Splunk TAs.

SC4S is essentially a maintained Splunk-oriented opinionated layer around this class of engine.

### Universal Forwarder reading files

A proven traditional pattern remains:

```text
device -> syslog-ng -> durable file -> Universal Forwarder -> Splunk
```

This is still a strong design when durable files are a requirement, HEC is undesirable, or the organization already has a mature file-based syslog platform.

The trade-off is that classification and normalization are now divided between the syslog daemon, file layout, UF configuration, and Splunk parsing tier.

### SC4S

SC4S is strongest when:

- Splunk is the primary destination;
- many network/security products send syslog;
- consistent vendor metadata matters;
- HEC is acceptable;
- you want a maintained parser catalog;
- you want persistent outage buffering without first writing every source to files;
- you need an extensible path from built-in sources to SIMPLE onboarding to custom log paths.

## 6. Benefits and costs

### Benefits

- vendor-aware source identification;
- recommended Splunk metadata out of the box;
- maintained source catalog;
- HEC-native output;
- persistent disk buffering;
- multiple HEC endpoints;
- metadata override model;
- dedicated ports for special sources;
- SIMPLE onboarding for well-formed unsupported sources;
- local custom filters/parsers/log paths;
- TLS support on input and output;
- health/status endpoint;
- built-in indexed `sc4s_*` fields;
- performance-tuning controls;
- archive capability;
- a common operational pattern across many syslog vendors.

### Costs

- another critical service to operate;
- container/runtime knowledge required;
- SC4S release behavior changes over time;
- built-in defaults may not match your index governance;
- HEC batch rejection can amplify one metadata mistake;
- regex-heavy parser paths consume CPU;
- UDP loss cannot be eliminated after the sender has transmitted the packet;
- SIMPLE paths can alter the raw shape expected by a downstream TA unless templates are chosen carefully;
- heavy customization can turn an upgradeable product into a local fork;
- HA for syslog is fundamentally awkward.

## 7. Edge collection beats centralizing everything

SC4S documentation recommends **edge collection** when possible.

Why?

UDP is send-and-forget. It does not know that a WAN is congested, a firewall state table is overloaded, or a central collector is unavailable. Even TCP syslog is not an application-level durable queue. Long paths increase the number of places where data can disappear.

A better design is often:

```text
Site A sources -> SC4S-A --\
Site B sources -> SC4S-B ----> Splunk HEC tier
Site C sources -> SC4S-C --/
```

rather than:

```text
every device across every WAN -> one central SC4S
```

The collector should be close to the sender when the source is high-value or high-volume.

Reference: [SC4S architecture](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)

## 8. UDP, TCP, RFC6587, and TLS: reliability without mythology

### UDP

UDP is attractive because it is simple and has low overhead.

But there is no retransmission. If any of these fill:

- NIC ring;
- kernel receive queue;
- SC4S input window;
- CPU capacity;

the packet can disappear.

`tcpdump` proving that a packet reached the interface does not prove syslog-ng consumed it.

Monitor:

```bash
netstat -su
ss -lunp
ethtool -S <nic>
```

### TCP

TCP gives flow control and retransmission at the transport layer. That is better than UDP for many security sources and for larger events.

But TCP does not create exactly-once end-to-end logging.

Data can still be lost:

- before a connection is established;
- in the sender when its own queue fills;
- during process restart;
- when application framing is wrong;
- when an application accepts bytes but later discards the message.

### RFC6587

Syslog over TCP needs framing. RFC6587 defines mechanisms used to separate messages on a stream. Some products call this "reliable syslog."

When a product explicitly supports RFC6587, use the SC4S RFC6587 listener rather than treating arbitrary TCP as equivalent.

### TLS

TLS adds confidentiality, server identity verification, and optionally client certificate controls.

Do not confuse:

```text
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=no
```

with a harmless troubleshooting toggle. Encryption without certificate verification can still permit an active machine-in-the-middle.

Production should normally use:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=yes
```

with the appropriate issuing CA in the SC4S trust path.

## 9. A practical production architecture

A strong baseline for an on-premises Splunk environment is:

```text
                          +-------------------+
FortiGate --UDP/TCP------>|                   |
Cisco -------TCP/TLS----->|       SC4S        |
DLP --------TCP---------->|                   |
Bitdefender--TCP--------->|                   |
                          +---------+---------+
                                    |
                                    | HTTPS HEC
                                    |
                         +----------v----------+
                         | HEC VIP / indexers  |
                         +----------+----------+
                                    |
                              indexer cluster
```

One SC4S instance can handle many vendors and many indexes.

You do **not** need:

```text
one SC4S per index
one HEC token per index
one incoming port per Splunk index
```

Those are different concerns.


### 9.1 Build order: do not start with the collector

A reliable implementation order is:

```text
1. inventory sources and expected EPS
2. define index and sourcetype policy
3. create Splunk indexes
4. create/test HEC
5. prepare Linux and storage
6. deploy SC4S
7. validate SC4S -> HEC with synthetic events
8. onboard one real source
9. validate metadata and field extraction
10. test HEC outage/buffering
11. load test
12. add sources incrementally
```

This ordering prevents a common failure pattern: starting SC4S first, sending production data immediately, and then discovering that the destination index does not exist or is not permitted by the HEC token.

### 9.2 Inventory before installation

Create a source table before touching configuration.

Example:

| Source | Protocol now | Desired protocol | EPS normal/peak | Avg bytes | Format | Target index | Expected sourcetype |
|---|---|---|---:|---:|---|---|---|
| FortiGate | UDP/5514 | RFC6587/TCP later | 3k/15k | 700 | FortiOS text | `fgt` | `fortigate_traffic` etc. |
| Bitdefender | TCP/1514 | TCP/1514 | 200/1k | 1400 | JSON in syslog | `av` | `bitdefender:gz` |
| DLP | TCP | TLS if supported | measure | measure | vendor-specific | `dlp` | vendor TA |
| Cisco | UDP/TCP | source-dependent | measure | measure | syslog | `cisco` | product-specific |

This table drives:

- storage sizing;
- protocol decisions;
- parser selection;
- HEC index authorization;
- performance tests;
- firewall rules.

### 9.3 Splunk side: create indexes first

If the final destination is an indexer cluster, create indexes through the cluster-manager bundle according to your Splunk operating model.

Conceptual example:

```ini
[fgt]
homePath = $SPLUNK_DB/fgt/db
coldPath = $SPLUNK_DB/fgt/colddb
thawedPath = $SPLUNK_DB/fgt/thaweddb
repFactor = auto

[av]
homePath = $SPLUNK_DB/av/db
coldPath = $SPLUNK_DB/av/colddb
thawedPath = $SPLUNK_DB/av/thaweddb
repFactor = auto
```

Do not invent retention in a copy/paste exercise. Apply the organization's approved:

- retention;
- size;
- volume;
- frozen/archive policy.

Validate the index before SC4S sends data:

```spl
| eventcount summarize=false index=fgt
```

or confirm effective index configuration through Splunk's normal administrative tooling.

### 9.4 Splunk side: configure HEC

A minimal HEC input for a controlled set of indexes:

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
indexes = main,fgt,av,dlp,cisco
useACK = 0
```

Two policies are possible.

**Restricted token**

```ini
indexes = main,fgt,av,dlp,cisco
```

Benefits:

- least privilege;
- explicit governance.

Cost:

- every new SC4S destination index must be added before data is enabled;
- one missed index can produce HEC 400 errors and batch loss.

**Unrestricted selected-index list**

This avoids an operational mismatch between SC4S's per-event index metadata and the token allow-list.

Benefits:

- simpler onboarding;
- lower risk of "Incorrect index" caused solely by token authorization.

Cost:

- broader HEC token privilege.

Choose deliberately.

### 9.5 Test HEC before installing SC4S

From the future collector host:

```bash
curl --cacert /path/to/ca.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

Expected:

```json
{"text":"HEC is healthy","code":17}
```

Then test the exact index:

```bash
read -rsp "HEC token: " HEC_TOKEN
echo

curl --fail-with-body \
  --cacert /path/to/ca.pem \
  -H "Authorization: Splunk ${HEC_TOKEN}" \
  -H "Content-Type: application/json" \
  https://splunk-hec.example.net:8088/services/collector/event \
  -d '{
    "index":"fgt",
    "sourcetype":"sc4s:preflight",
    "event":"SC4S HEC preflight"
  }'

unset HEC_TOKEN
```

Do this for every restricted index before onboarding its source.

### 9.6 Ubuntu/Linux preparation

Record the baseline:

```bash
cat /etc/os-release
uname -a
timedatectl
ss -lntup
df -hT
df -i
```

If host `syslog-ng` or `rsyslog` already owns the ports SC4S will use, do not immediately purge it.

Back up configuration, then stop/disable/mask the conflicting listener so rollback remains possible during migration.

Example:

```bash
systemctl stop syslog-ng
systemctl disable syslog-ng
systemctl mask syslog-ng
```

Do not disable `systemd-journald` merely because SC4S is being installed. Host journaling and network syslog reception are separate concerns.

### 9.7 Kernel baseline

SC4S runtime guidance calls out receive-buffer tuning and IPv4 forwarding.

Example baseline:

```bash
cat >/etc/sysctl.d/90-sc4s.conf <<'EOF'
net.core.rmem_default = 17039360
net.core.rmem_max = 17039360
net.ipv4.ip_forward = 1
EOF

sysctl --system
```

Validate:

```bash
sysctl net.core.rmem_default
sysctl net.core.rmem_max
sysctl net.ipv4.ip_forward
```

For high-load tuning, use measured values later rather than starting with extreme buffers.

### 9.8 Directory and persistent-volume layout

Typical host paths:

```text
/opt/sc4s/
  env_file
  local/
    context/
    config/
  archive/
  tls/
```

Create:

```bash
install -d -m 0750 /opt/sc4s
install -d -m 0750 /opt/sc4s/local
install -d -m 0750 /opt/sc4s/archive
install -d -m 0750 /opt/sc4s/tls
```

Create a persistent container volume:

```bash
docker volume create splunk-sc4s-var
docker volume inspect splunk-sc4s-var
```

The persistent volume is important because SC4S disk-buffer state must survive an ordinary container restart.

Check the backing filesystem:

```bash
df -hT /var/lib/docker
df -i /var/lib/docker
```

If buffer capacity requires hundreds of gigabytes or terabytes, solve the storage architecture before production.

### 9.9 TLS trust for HEC

The trust relationship should normally be:

```text
SC4S trusts CA
       |
       v
CA signs HEC server certificate
       |
       v
HEC URL hostname/IP matches certificate SAN
```

Place the appropriate CA certificate/chain in the SC4S TLS mount according to the deployment method.

Validate with OpenSSL:

```bash
openssl s_client \
  -connect splunk-hec.example.net:8088 \
  -showcerts </dev/null
```

Then with curl:

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

Do not enable `TLS_VERIFY=no` simply to make a certificate problem disappear.

### 9.10 Create the initial `env_file`

Example:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_URL=https://splunk-hec.example.net:8088
SC4S_DEST_SPLUNK_HEC_DEFAULT_TOKEN=<SECRET>
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=yes

SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_ENABLE=yes
SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_RELIABLE=no

SC4S_LISTEN_DEFAULT_UDP_PORT=5514
SC4S_LISTEN_STATUS_HOST=127.0.0.1
```

Permissions:

```bash
chown root:root /opt/sc4s/env_file
chmod 0600 /opt/sc4s/env_file
```

Syntax check:

```bash
grep -nEv \
'^[[:space:]]*($|#|[A-Za-z_][A-Za-z0-9_]*=.*)$' \
/opt/sc4s/env_file
```

Expected: no output.

### 9.11 Systemd and container runtime

A typical service mounts:

- persistent syslog-ng state;
- local overrides;
- optional archive;
- TLS material.

Online deployments may use an approved pinned SC4S image reference.

Air-gapped deployments should use a locally loaded/tagged image and:

```text
--pull=never
```

Do not make an offline service depend on:

```text
ExecStartPre=docker pull ...
```

Validate the unit:

```bash
systemd-analyze verify /etc/systemd/system/sc4s.service
systemctl daemon-reload
systemctl enable sc4s
systemctl start sc4s
```

### 9.12 First startup validation

Check:

```bash
systemctl status sc4s --no-pager -l
docker ps --filter name=SC4S
docker logs --tail 200 SC4S
```

Expected milestones include:

```text
HEC connection test successful
SC4S version=...
health/status process started
syslog-ng started
```

Then:

```bash
docker exec SC4S \
  syslog-ng-ctl healthcheck --timeout 5
```

And:

```bash
docker exec SC4S syslog-ng-ctl stats \
  | grep 'dst.http;d_hec_fmt'
```

Do not onboard a real source while SC4S startup is already producing HEC 400/401/TLS failures.

### 9.13 Confirm listeners

UDP example:

```bash
ss -lunp | grep ':5514'
```

TCP example:

```bash
ss -lntp | grep ':1514'
```

If a configured listener is missing:

1. validate `env_file`;
2. check container environment;
3. check preprocessed config;
4. check port conflicts;
5. inspect SC4S startup logs.

### 9.14 First synthetic event

A generic UDP test proves transport, not vendor classification:

```bash
logger \
  --udp \
  --server <SC4S_IP> \
  --port 5514 \
  --tag sc4s-test \
  "SC4S-SYNTHETIC-001"
```

Search broadly:

```spl
index=* "SC4S-SYNTHETIC-001"
| table _time index host source sourcetype _raw
```

Then send a vendor-representative sanitized sample or enable a low-risk real source.

### 9.15 First real source acceptance

For each source prove:

```text
packet/connection reaches collector
listener accepts it
SC4S identifies correct vendor/product
correct index
correct sourcetype
correct host
correct timestamp
expected _raw shape
TA field extraction works
HEC dropped counter does not increase
```

Only after that should the source be considered onboarded.


## 10. One HEC token, many indexes

SC4S sets the index per event.

Example HEC event:

```json
{
  "index": "fgt",
  "sourcetype": "fortigate_traffic",
  "host": "fgt-01",
  "event": "..."
}
```

The HEC input setting:

```ini
index = main
```

means "use `main` if the event does not specify an index."

It does **not** override the event-level `"index":"fgt"` field.

The HEC `indexes` list is authorization:

```ini
indexes = main,fgt,av,dlp,cisco
```

A scalable token might therefore be:

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
indexes = main,fgt,av,dlp,cisco
useACK = 0
```

SC4S documentation warns that if an event specifies an index that the token cannot use, HEC returns HTTP 400. Because SC4S batches multiple events, one bad event can cause collateral loss in that batch.

Operational rule:

> Create the Splunk index, authorize it on the HEC token, test it manually, then enable the new SC4S route.

Do not enable HEC indexer acknowledgement for SC4S unless current SC4S documentation explicitly changes its support position. The syslog-ng HTTP destination has historically not supported Splunk HEC ACK semantics.

## 11. Why SC4S ships with `netfw`, `netops`, `netdlp`, and similar indexes

SC4S defaults are a taxonomy, not a law.

For example, FortiOS defaults historically map categories such as traffic/UTM to network-firewall indexes and event/system categories to network-operations indexes.

That creates a useful zero-configuration onboarding experience.

But your organization may require:

```text
FortiGate -> fgt
Bitdefender -> av
DLP -> dlp
Cisco -> cisco
```

That is valid.

The index naming policy is your governance decision. SC4S's job is to route consistently.

## 12. The metadata override model

SC4S maintains an internal metadata mapping.

A reference copy is deposited at:

```text
/opt/sc4s/local/context/splunk_metadata.csv.example
```

Do not edit the `.example` file.

Create or edit:

```text
/opt/sc4s/local/context/splunk_metadata.csv
```

The format is:

```csv
key,metadata,value
```

Supported metadata includes:

- `index`;
- `source`;
- `host`;
- `sourcetype`;
- `sc4s_template`.

SC4S documentation recommends overriding the index most often and changing sourcetype/template only when you understand the downstream TA implications.

Reference: [SC4S configuration — metadata overrides](https://splunk.github.io/splunk-connect-for-syslog/main/configuration/)

## 13. Example: override FortiGate into your own index

If your policy requires all FortiGate events in `fgt`:

```csv
fortinet_fortios_traffic,index,fgt
fortinet_fortios_utm,index,fgt
fortinet_fortios_event,index,fgt
fortinet_fortios_log,index,fgt
```

Check the exact keys for your release:

```bash
grep '^fortinet_fortios_' \
  /opt/sc4s/local/context/splunk_metadata.csv.example
```

Why check every release?

Because `.example` reflects the internal mapping shipped with that image. It can change.

The FortiOS source documentation is also a reference:

- [SC4S Fortinet FortiOS source](https://splunk.github.io/splunk-connect-for-syslog/main/sources/vendor/Fortinet/fortios/)

## 14. Sourcetype overrides are more dangerous than index overrides

A custom index changes data placement.

A custom sourcetype can change parsing semantics.

A Splunk TA may expect:

```text
fortigate_traffic
```

or a version-specific alternate naming convention.

If you arbitrarily rename it:

```csv
fortinet_fortios_traffic,sourcetype,my_firewall
```

the TA's:

- field extractions;
- aliases;
- eventtypes;
- tags;
- CIM mappings;

may stop applying.

Index overrides are organizational. Sourcetype overrides are application contracts.

Treat them differently.

## 15. SIMPLE sources: the fast onboarding bridge

SC4S provides a SIMPLE log path for a source that:

- is not already supported;
- sends well-formed RFC5424 or a common RFC3164 variant;
- can use a dedicated port;
- needs quick routing to a known index/sourcetype.

Example:

```csv
bitdefender_gz,index,av
bitdefender_gz,sourcetype,bitdefender:gz
```

and:

```ini
SC4S_LISTEN_SIMPLE_BITDEFENDER_GZ_TCP_PORT=1514
```

The naming must match:

```text
metadata key       bitdefender_gz
environment name   BITDEFENDER_GZ
```

Reference: [SC4S SIMPLE source](https://splunk.github.io/splunk-connect-for-syslog/main/sources/simple/)

Important:

> SIMPLE is intentionally an interim onboarding mechanism. When a source needs deeper parsing, enrichment, normalization, or special raw-message handling, move to a dedicated log path.

## 16. Why JSON can stop parsing after SC4S is inserted

This is a classic integration problem.

Before SC4S:

```text
Bitdefender -> HF
_raw = {"field":"value", ...}
```

The TA sees a pure JSON document.

After a generic syslog layer:

```text
Bitdefender -> SC4S -> HF
_raw = Aug 24 08:00:00 host program: {"field":"value", ...}
```

That is no longer a pure JSON document.

`KV_MODE=json`, `spath`, or a vendor TA may expect the first meaningful character to be `{`.

The transport is working. The parsing contract is not.

This is why SC4S templates matter.

## 17. Built-in output templates you should understand

SC4S uses syslog-ng/AxoSyslog templates to decide what becomes the Splunk event body.

Important built-ins include:

| Template | Concept |
|---|---|
| `t_standard` | Normal date/host/header/message style |
| `t_msg_only` | Send only `${MSGONLY}` |
| `t_msg_trim` | Send `${MSGONLY}` with surrounding whitespace stripped |
| `t_hdr_msg` | Header + message |
| `t_legacy_hdr_msg` | Legacy header + message |
| `t_hdr_sdata_msg` | Header + RFC5424 structured data + message |
| `t_program_msg` | Program/PID + message |
| `t_JSON_3164` | JSON representation of RFC3164-related macros |
| `t_JSON_5424` | JSON representation of RFC5424-related macros |

Current reference:

- [SC4S configuration templates](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/configuration.md)
- [AxoSyslog templates and macros](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/customizing-message-format/configuring-macros/)

For JSON-in-syslog, a useful override is:

```csv
bitdefender_gz,sc4s_template,t_msg_trim
```

The pipeline becomes:

```text
RFC syslog envelope + JSON MESSAGE
          |
          v
SC4S parses the envelope
          |
          v
t_msg_trim
          |
          v
pure JSON MESSAGE
          |
          v
HEC _raw
```

Verify:

```spl
index=av sourcetype="bitdefender:gz"
| eval first_character=substr(trim(_raw),1,1)
| stats count by first_character
```

Expected for JSON:

```text
{
```

## 18. Preserve raw first, normalize second

When onboarding a source, capture what actually arrives before writing parsing rules.

Useful commands:

```bash
tcpdump -ni any -s0 -A -c 10 'tcp port 1514'
tcpdump -ni any -s0 -A -c 10 'udp port 5514'
```

For development only, SC4S provides raw-message storage controls. Do not leave raw-message capture enabled in production: it has substantial memory/disk overhead.

The safest development loop is:

```text
capture raw
-> classify transport/framing
-> identify syslog envelope
-> identify vendor body
-> compare expected Splunk TA sourcetype
-> choose template
-> test field extraction
-> only then normalize further
```

## 19. Creating your own SC4S log path

When SIMPLE is no longer enough, SC4S supports local custom parser/log-path development under the mounted local directory.

The runtime documentation points to the local configuration structure under:

```text
/opt/sc4s/local/config/
```

Use the shipped examples as a starting point.

A custom SC4S parser conceptually has two parts:

1. an `application` filter that identifies the source;
2. a parser block that sets metadata and performs message handling.

SC4S parser documentation shows the pattern:

```text
incoming message
    |
application filter
    |
custom parser
    |
r_set_splunk_dest_default(...)
    |
template + metadata
```

References:

- [SC4S runtime configuration](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-runtime-configuration/)
- [Creating SC4S parsers](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/)
- [Filtering messages](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/filter_message/)
- [Parsing messages](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/parse_message/)

## 20. Parser design: identify narrowly

A parser should not match because a message contains a common word.

Bad concept:

```text
message contains "Firewall"
```

Better:

```text
specific RFC5424 SD-ID
specific program
specific vendor prefix
specific stable message signature
source IP + payload signature when unavoidable
```

Over-broad filters create silent misclassification.

The most dangerous parsing error is often not "no match." It is "wrong match."

## 21. Rewriting and trimming raw messages

Because SC4S uses the syslog-ng/AxoSyslog engine, you can use rewrite rules when you need controlled message modification.

AxoSyslog supports `subst()` for regex or string replacement on soft macros such as `MESSAGE`.

Conceptual example:

```conf
rewrite r_remove_vendor_prefix {
    subst(
        "^PREFIX:[[:space:]]*",
        "",
        value("MESSAGE")
    );
};
```

A simple string replacement is generally cheaper than a complex regex. Do not use regex because it is familiar; use it when the structure actually requires it.

Reference:

- [AxoSyslog rewrite rules](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/modifying-messages/)
- [Replace message parts](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/modifying-messages/rewrite-replace/)

## 22. Extracting fields with regex

AxoSyslog provides `regexp-parser()`.

Named capture groups create name-value pairs.

Concept:

```conf
parser p_vendor {
    regexp-parser(
        patterns(
            "^device=(?<device>[^ ]+) action=(?<action>[^ ]+)"
        )
        prefix("vendor.")
        template("${MESSAGE}")
    );
};
```

You can then use the extracted fields in:

- conditions;
- metadata decisions;
- templates;
- indexed HEC fields.

Reference: [AxoSyslog regexp parser](https://axoflow.com/docs/axosyslog-core/chapter-parsers/parser-regexp/)

But be conservative.

Complex PCRE on every event can become a CPU bottleneck at high EPS. Prefer:

- native SC4S parser support;
- structured data;
- delimiter parsers;
- key-value parsers;
- JSON parsers;
- simple string/prefix checks;

before expensive regular expressions.

## 23. Templates give you control over the final Splunk `_raw`

A custom template can combine macros and parsed fields.

AxoSyslog template concept:

```conf
template t_example {
    template("${ISODATE} ${HOST} ${MESSAGE}\n");
};
```

SC4S local parser design can select a template through its Splunk destination rewrite logic.

The design question is:

> What should the downstream Splunk TA see as `_raw`?

Not:

> What output format looks nicest in tcpdump?

If a TA expects pure JSON, preserve pure JSON. If a TA expects a vendor prefix, do not trim it. If the timestamp is parsed into HEC `time`, you may not need to retain it in `_raw`.

## 24. Conditional metadata by host, IP, subnet, or compliance scope

Sometimes vendor-level metadata is too coarse.

Example:

```text
same firewall product
production devices -> pci_firewall
lab devices        -> lab_firewall
```

SC4S supports compliance/source-based override files in its local context area. Filters can match host or netmask, and the corresponding CSV can override `.splunk.index`, `.splunk.source`, `.splunk.sourcetype`, or add indexed fields.

This is useful for:

- PCI scope;
- geography;
- security zones;
- regulated environments;
- acquisition/migration boundaries.

Do not duplicate a whole parser just to change an index for one subnet.

Reference: [SC4S metadata/compliance overrides](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/configuration.md)

## 25. Disk buffering: what it protects and what it cannot protect

SC4S disk buffering protects the segment:

```text
SC4S -> Splunk HEC
```

It cannot recover a UDP datagram that never reached SC4S.

When all HEC destinations are unavailable, SC4S can queue events locally and drain them later.

The approximate sizing model documented by SC4S is:

```text
required bytes
≈ peak EPS
× average event bytes
× outage seconds
× ~1.7 syslog-ng overhead
```

Example:

```text
20,000 EPS
× 800 bytes
× 14,400 seconds (4 hours)
× 1.7
≈ 391.7 GB
```

Provision more than the mathematical minimum.

The system also needs enough post-outage throughput to drain the queue:

```text
maximum output throughput > normal incoming rate
```

Otherwise the buffer technically works but never catches up.

SC4S recommends normal disk buffering over "reliable" disk buffering for this use because reliable mode imposes significant performance cost with limited practical benefit.

Reference: [SC4S disk buffering](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/configuration.md)

## 26. HTTP 400 is different from an outage

This distinction is operationally critical.

A network outage or HTTP 503 is transient. Buffering/retry is appropriate.

HTTP 400 means the request is invalid.

Example:

```json
{"text":"Incorrect index","code":7}
```

SC4S may treat this as non-retryable and drop the affected batch.

This is why:

```text
new index
-> create in Splunk
-> permit on token
-> curl test
-> metadata override
-> SC4S restart
-> production traffic
```

is safer than enabling a source first.

## 27. Monitoring the HEC destination

Useful counters:

```bash
docker exec SC4S syslog-ng-ctl stats \
  | grep 'dst.http;d_hec_fmt'
```

Interpretation:

```text
written   successfully delivered
queued    waiting
dropped   discarded
```

A historical nonzero `dropped` value is evidence, not necessarily a current fault.

Watch the delta:

```bash
watch -n 5 \
  "docker exec SC4S syslog-ng-ctl stats | grep 'dst.http;d_hec_fmt'"
```

Healthy steady state:

```text
written -> increasing
queued  -> near zero
dropped -> not increasing
```

## 28. SC4S health is not the same as data health

This can return healthy:

```bash
docker exec SC4S \
  syslog-ng-ctl healthcheck --timeout 5
```

while a vendor stream is still being rejected by HEC.

Healthcheck answers:

```text
is the engine/main loop healthy?
```

It does not prove:

```text
every source is classified correctly
every index exists
every HEC authorization is valid
every TA is extracting fields
```

Use layered health checks.

## 29. The status endpoint on TCP/8080

SC4S runs a status/health HTTP endpoint, default port 8080.

Current SC4S also supports changing the bind host.

If only local monitoring needs it:

```ini
SC4S_LISTEN_STATUS_HOST=127.0.0.1
```

This is better than exposing a plain HTTP status service on every interface and relying only on perimeter filtering.

Reference: [SC4S configuration — status host/port](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/configuration.md)

## 30. Air-gapped deployment

An air-gapped SC4S host should not execute a registry pull on every service restart.

Bad for an offline system:

```ini
ExecStartPre=/usr/bin/docker pull ${SC4S_IMAGE}
```

The service can fail before the locally cached image is started.

A better offline lifecycle is:

```text
connected staging system
-> obtain approved image
-> verify digest/signature according to policy
-> docker save / official offline archive
-> controlled media transfer
-> checksum verification
-> docker load
-> local tag
-> --pull=never
```

Example:

```bash
docker load < sc4s-image.tar
docker tag <loaded-image> sc4slocal:approved
```

Systemd:

```text
Environment="SC4S_IMAGE=sc4slocal:approved"
```

and:

```text
docker run --pull=never ...
```

Keep the persistent volume separate from the ephemeral container image.

## 31. Baseline host preparation

At minimum verify:

```bash
cat /etc/os-release
uname -a
timedatectl
chronyc tracking
ss -lntup
df -hT
df -i
```

SC4S runtime guidance recommends tuning Linux receive buffers because distribution defaults can be too small for high-volume UDP.

Typical sysctl baseline:

```ini
net.core.rmem_default = 17039360
net.core.rmem_max = 17039360
net.ipv4.ip_forward = 1
```

Do not copy tuning values blindly into a high-EPS system. Benchmark them.

Reference: [SC4S runtime configuration](https://github.com/splunk/splunk-connect-for-syslog/blob/main/docs/gettingstarted/getting-started-runtime-configuration.md)

## 32. A clean `env_file` baseline

Example:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_URL=https://splunk-hec.example.net:8088
SC4S_DEST_SPLUNK_HEC_DEFAULT_TOKEN=<SECRET>
SC4S_DEST_SPLUNK_HEC_DEFAULT_TLS_VERIFY=yes

SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_ENABLE=yes
SC4S_DEST_SPLUNK_HEC_DEFAULT_DISKBUFF_RELIABLE=no

# FortiGate example
SC4S_LISTEN_DEFAULT_UDP_PORT=5514
SC4S_OPTION_FORTINET_SOURCETYPE_PREFIX=fortigate

# Bitdefender SIMPLE example
SC4S_LISTEN_SIMPLE_BITDEFENDER_GZ_TCP_PORT=1514

# Restrict local health endpoint where appropriate
SC4S_LISTEN_STATUS_HOST=127.0.0.1
```

Keep comments valid:

```text
# comment
```

not:

```text
\# comment
```

Validate an env file:

```bash
grep -nEv \
'^[[:space:]]*($|#|[A-Za-z_][A-Za-z0-9_]*=.*)$' \
/opt/sc4s/env_file
```

Expected: no output.

## 33. Secrets

Treat the HEC token as a credential.

Do not:

- commit it to Git;
- paste it into tickets or screenshots;
- leave it in shell history;
- reuse one token across unrelated trust zones without reason.

Use file permissions:

```bash
chown root:root /opt/sc4s/env_file
chmod 0600 /opt/sc4s/env_file
```

If a token is exposed, rotate it.

## 34. High-load ingestion: find the actual bottleneck first

Do not tune random knobs after seeing dropped events.

A syslog path has multiple queues:

```text
sender queue
NIC
kernel socket buffer
SC4S source
SC4S input window
parser CPU
HEC worker queue
disk buffer
network to Splunk
HEC endpoint
Splunk ingestion queues
indexer storage
```

Measure each layer.

### Network/kernel

```bash
netstat -su
ss -s
ethtool -S <nic>
sar -n DEV 1
```

### CPU

```bash
mpstat -P ALL 1
pidstat -p $(pgrep -f syslog-ng | head -1) 1
```

### Memory

```bash
free -h
vmstat 1
```

### Disk

```bash
iostat -xz 1
df -h
```

### SC4S

```bash
docker exec SC4S syslog-ng-ctl stats
docker exec SC4S syslog-ng-ctl healthcheck --timeout 5
docker logs --since 10m SC4S
```

### Splunk

Search `_internal` for HEC errors, queue pressure, and indexing delays.

## 35. Receive-buffer tuning

SC4S documentation recommends increasing receive buffers when bursts overflow the default capacity.

Host:

```ini
net.core.rmem_default = 536870912
net.core.rmem_max = 536870912
```

SC4S example:

```ini
SC4S_SOURCE_UDP_SO_RCVBUFF=536870912
SC4S_SOURCE_TCP_SO_RCVBUFF=536870912
SC4S_SOURCE_RFC6587_SO_RCVBUFF=536870912
```

The documentation reports substantial performance improvement in its lab, but this is not a universal sizing rule.

Larger buffers:

- absorb bursts;
- consume memory;
- can hide a sustained throughput deficit by increasing latency.

A buffer is not capacity. It is time.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## 36. UDP input windows

SC4S exposes UDP input-window tuning.

Example:

```ini
SC4S_SOURCE_UDP_IW_USE=yes
SC4S_SOURCE_UDP_IW_SIZE=1000000
```

This allows syslog-ng to hold more messages in application memory during temporary downstream slowdown.

It does not increase baseline sustainable throughput.

Once the window is full, the kernel queue fills next. After that, UDP drops.

Treat input windows as burst protection.

## 37. Fetch limits

Fetch limit controls how many events are fetched in a read cycle.

Examples:

```ini
SC4S_SOURCE_UDP_FETCH_LIMIT=1000
SC4S_SOURCE_TCP_FETCH_LIMIT=2000
```

Too low:

- excessive loop overhead;
- underutilized buffers.

Too high:

- a source can monopolize processing;
- a read can fill too much of the input window.

Tune it with the window and real workload.

## 38. Multiple UDP sockets and `SO_REUSEPORT`

SC4S can open multiple sockets on one UDP port.

Example:

```ini
SC4S_SOURCE_LISTEN_UDP_SOCKETS=32
```

Without eBPF, Linux generally hashes a flow/source to a socket. This preserves ordering better but can leave one CPU hot when one source dominates traffic.

This optimization is strongest when many senders contribute traffic.

## 39. eBPF for a dominant UDP stream

SC4S supports eBPF-assisted distribution for UDP.

Example:

```ini
SC4S_SOURCE_LISTEN_UDP_SOCKETS=32
SC4S_ENABLE_EBPF=yes
SC4S_EBPF_NO_SOCKETS=32
```

This can distribute packets from a single heavy sender across workers more evenly.

Trade-off:

- improved parallelism;
- packet processing order can change;
- privileged container requirements;
- more operational complexity.

SC4S publishes benchmark data showing large improvement under specific lab conditions. Use that as evidence that the feature matters, not as a throughput guarantee for your hardware.

Reference: [SC4S fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)

## 40. TCP parallelization

A single very busy TCP connection can become a serialization point.

SC4S supports:

```ini
SC4S_ENABLE_PARALLELIZE=yes
SC4S_PARALLELIZE_NO_PARTITION=4
```

This is useful when one TCP stream dominates.

If you already have many concurrent TCP connections, parallelization can add overhead without meaningful gain.

Again: benchmark.

## 41. HEC workers

SC4S HEC destinations have worker controls. Current documentation lists ten workers as the default and recommends changing them only for unusually high or low volume with proper testing.

Example:

```ini
SC4S_DEST_SPLUNK_HEC_DEFAULT_WORKERS=10
```

Do not automatically set this equal to CPU count. HEC performance depends on:

- indexer capacity;
- latency;
- event size;
- TLS;
- batching;
- disk buffering;
- destination count.

## 42. SC4S Lite

Parser evaluation costs CPU.

If you know exactly which vendors you ingest, SC4S Lite can reduce the parser surface and improve capacity in real workloads.

This is a useful A/B test when CPU is dominated by classification rather than network I/O.

## 43. When a dedicated SC4S instance is justified

One SC4S instance can handle many vendors.

But SC4S documentation recommends considering a dedicated service/host when one source produces a very large percentage of total traffic.

Good reasons for another instance:

- one dominant high-EPS firewall;
- security-zone separation;
- geographic edge collection;
- different operational ownership;
- different maintenance windows;
- failure-domain isolation;
- a special parser with significant CPU cost.

Bad reason:

```text
"I need another Splunk index."
```

## 44. Front-side load balancers are not the normal scaling answer

SC4S documentation is intentionally cautious about load balancing **between sources and SC4S**.

Reasons include:

- source IP can be lost;
- UDP has no session semantics;
- long TCP connections distribute poorly;
- the LB can become another loss point;
- hashing can create uneven load;
- source devices cannot always fail over intelligently.

Prefer vertical scaling and edge placement first.

HEC output load balancing is different. SC4S can use a HEC VIP or multiple HEC URLs because HTTP has better request/response behavior.

Reference: [SC4S load balancer guidance](https://splunk.github.io/splunk-connect-for-syslog/latest/architecture/lb/)

## 45. Host networking

SC4S systemd container examples commonly use:

```text
--network host
```

Benefits:

- simple listener behavior;
- no NAT/port publishing layer;
- source networking is easier to reason about;
- many ports can be opened without adding `-p` mappings.

Cost:

- the container shares the host network namespace;
- port conflicts occur directly on the host;
- network isolation is reduced.

Reference: [Docker host network](https://docs.docker.com/engine/network/drivers/host/)

## 46. Where macvlan can help

`macvlan` gives a container its own MAC and IP on the physical network.

Concept:

```text
physical VLAN
   |
   +-- SC4S container MAC/IP
   |
   +-- other hosts
```

This can be useful when:

- legacy appliances expect a collector to look like a physical host;
- you want a dedicated collector IP independent of the Docker host IP;
- you need to avoid host-port collisions;
- network policy is based on L2/L3 identity;
- you want multiple collector identities on separate VLANs.

It can also be useful in advanced migration patterns where old devices cannot easily change destination addressing.

But macvlan is **not** an SC4S high-throughput magic switch.

It does not fix:

- CPU-bound regex parsers;
- too-small socket buffers;
- HEC rejection;
- slow Splunk indexers;
- disk-buffer saturation;
- a sender that overloads one TCP stream.

Reference: [Docker macvlan](https://docs.docker.com/engine/network/drivers/macvlan/)

## 47. Macvlan trade-offs

Docker documents important limitations:

- Linux only;
- often blocked by cloud providers;
- switch/NIC must tolerate multiple MAC addresses/promiscuous behavior;
- too many MACs can create "VLAN spread";
- macvlan containers cannot communicate directly with the host by default because of a Linux kernel restriction.

That last point surprises operators.

A host-side macvlan interface or a second bridge network may be needed if host-to-container communication is required.

If the environment restricts multiple MAC addresses, consider Docker `ipvlan`.

## 48. Macvlan example pattern

Example only — adapt addresses and parent interface:

```bash
docker network create -d macvlan \
  --subnet=192.0.2.0/24 \
  --gateway=192.0.2.1 \
  -o parent=ens192 \
  sc4s_l2
```

Run the container with an assigned address:

```bash
docker run \
  --network sc4s_l2 \
  --ip 192.0.2.50 \
  ...
```

Before adopting this, validate:

```text
switch port security
MAC limits
promiscuous/multiple-MAC support
VLAN design
routing
monitoring access
HEC egress
host-to-container requirement
HA behavior
```

Macvlan is an architecture tool, not a default deployment recommendation.

## 49. High availability without pretending syslog becomes lossless

Syslog HA is difficult because the sender often has the least sophisticated failover logic in the architecture.

A VIP does not automatically make UDP reliable.

SC4S documentation discusses HA approaches and warns against conventional front-side load balancing. Modern SC4S guidance includes MicroK8s/MetalLB approaches for specific HA requirements.

Simpler operational designs can sometimes preserve more data:

- edge collectors;
- VM HA/vMotion;
- redundant source destinations when the appliance supports them;
- sender-side TCP/TLS with internal queues;
- fast collector recovery;
- adequate local HEC disk buffer;
- configuration-as-code to rebuild quickly.

Define the failure you are trying to survive before selecting an HA product.

## 50. Archive is different from disk buffer

Disk buffer is temporary delivery resilience.

Archive is intentional local retention.

SC4S supports archive output and documents compliance/diode modes.

If you enable archive:

- size the filesystem;
- implement rotation;
- monitor capacity;
- define retention;
- secure the files;
- understand that SC4S does not automatically prune them for you.

Do not call the disk buffer an archive.

## 51. Timezones

Avoid a global timezone setting copied from a forum.

`SC4S_DEFAULT_TIMEZONE` applies to events that lack a usable timezone.

If a source already sends:

```text
tz="+0330"
```

that information should drive event time.

A global timezone override is appropriate only when you know the affected legacy sources share that timezone.

Check:

```spl
index=<index>
| eval ingest_delay=_indextime-_time
| stats avg(ingest_delay) max(ingest_delay) by sourcetype
```

Large positive/negative delays can reveal timestamp mistakes.

## 52. A layered troubleshooting methodology

Never start by restarting everything.

Follow the event.

### Layer 1 — sender

Questions:

- Is logging enabled?
- Correct destination IP?
- Correct port?
- Correct protocol?
- Correct facility/severity filters?
- Does the sender maintain a queue?
- Does it report drops?

### Layer 2 — network

```bash
tcpdump -ni any host <SOURCE_IP> and port <PORT>
```

If no packets/connection arrive, SC4S is not the problem yet.

### Layer 3 — listener

UDP:

```bash
ss -lunp | grep ':5514'
```

TCP:

```bash
ss -lntp | grep ':1514'
```

If `tcpdump` sees packets but no process owns the port, fix listener configuration.

### Layer 4 — SC4S source counters

```bash
docker exec SC4S syslog-ng-ctl stats
```

Compare counters before and after a controlled event.

### Layer 5 — classification

Search SC4S logs and Splunk indexed `sc4s_*` fields:

```spl
index=* sc4s_fromhostip="<SOURCE_IP>"
| stats count by sc4s_vendor sc4s_product sourcetype index
```

### Layer 6 — HEC

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

Test the exact destination index:

```bash
curl --fail-with-body \
  --cacert /opt/sc4s/tls/trusted.pem \
  -H "Authorization: Splunk $HEC_TOKEN" \
  -H "Content-Type: application/json" \
  https://splunk-hec.example.net:8088/services/collector/event \
  -d '{"index":"fgt","event":"hec-index-test"}'
```

### Layer 7 — Splunk parsing

```spl
index=<target> host=<host>
| table _time _indextime index host source sourcetype _raw
```

Then validate field extraction.

## 53. Common failure: "Incorrect index"

SC4S log:

```text
status_code='400'
response='{"text":"Incorrect index","code":7}'
```

Check:

1. Does the event payload explicitly contain `"index":"..."`?
2. Does that index exist?
3. Is the index allowed by the HEC token?
4. Was the HEC configuration reloaded/restarted as required?
5. Did SC4S metadata override load?
6. Is another event in the same HEC batch using a different unauthorized index?

Do not assume the `index =` default in `inputs.conf` forces SC4S events into that index.

## 54. Common failure: `sc4s:events` shows dropped batches

An internal event may show:

```text
Message(s) dropped while sending message to destination
```

and the displayed HEC request may itself use `index=main`.

Do not conclude that `main` is invalid.

Look for:

```text
invalid-event-number
batch_size
```

The invalid event can be another member of the batch.

This is why HEC allow-list governance must track every SC4S destination index.

## 55. Common failure: packet visible in tcpdump, nothing in Splunk

Checklist:

```bash
ss -lunp
ss -lntp
docker exec SC4S syslog-ng-ctl healthcheck --timeout 5
docker exec SC4S syslog-ng-ctl stats
docker logs --since 10m SC4S
netstat -su
```

Then:

- confirm correct protocol;
- confirm listener port;
- confirm firewall;
- confirm classification;
- confirm HEC destination counters.

`tcpdump` is only one layer.

## 56. Common failure: JSON becomes "raw text"

Capture one event before and after SC4S.

Check `_raw`.

If it changed from:

```json
{"event":"..."}
```

to:

```text
timestamp host program: {"event":"..."}
```

use an appropriate template such as:

```csv
vendor_product,sc4s_template,t_msg_trim
```

provided the JSON is the syslog message body.

Then verify:

```spl
index=<index> sourcetype=<sourcetype>
| spath
| fieldsummary
```

If the sender transmits raw JSON with **no syslog envelope**, SIMPLE is the wrong abstraction. Use a dedicated custom/no-parse path or retain direct ingestion.

## 57. Common failure: TLS warning or certificate mismatch

Validate independently of SC4S:

```bash
openssl s_client \
  -connect splunk-hec.example.net:8088 \
  -showcerts </dev/null
```

Then:

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

The trust file should normally contain the issuing CA chain, not a casually copied server private-key bundle.

Certificate identity must match the hostname or IP used in the URL.

## 58. Common failure: SC4S cannot restart in an air gap

If systemd contains:

```text
docker pull ghcr.io/...
```

every restart depends on internet access.

Fix the lifecycle:

```text
local approved image
+ no pull pre-step
+ --pull=never
```

Check effective service:

```bash
systemctl cat sc4s
systemctl show sc4s -p ExecStart -p ExecStartPre -p Environment
```

## 59. Common failure: listener port missing after restart

Check env syntax:

```bash
grep -nEv \
'^[[:space:]]*($|#|[A-Za-z_][A-Za-z0-9_]*=.*)$' \
/opt/sc4s/env_file
```

Check actual container environment:

```bash
docker inspect SC4S \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^SC4S_LISTEN'
```

Check generated config where useful:

```bash
docker exec SC4S \
  syslog-ng-ctl config --preprocessed \
  | grep -n -C 5 '<PORT>'
```

Check for port collisions.

## 60. Common failure: disk buffer does not drain

Determine whether the destination is actually healthy:

```bash
curl ...
```

Then inspect:

```bash
docker exec SC4S syslog-ng-ctl stats
df -h
iostat -xz 1
```

A buffer drains only if:

```text
current output capacity > incoming rate
```

If new events arrive at 40k EPS and the recovered path can forward only 35k EPS, the queue cannot shrink.

## 61. Performance testing

SC4S recommends testing your own workload.

The project uses `loggen` for synthetic benchmarks.

UDP concept:

```bash
loggen \
  --interval 60 \
  --rate 27000 \
  -s 1000 \
  --no-framing \
  --dgram \
  <SC4S_IP> 514
```

Measure:

- sent count;
- SC4S received count;
- Splunk indexed count;
- latency;
- kernel receive errors;
- CPU;
- memory;
- HEC queue;
- disk-buffer behavior.

A throughput number without loss and latency measurements is incomplete.

Reference: [SC4S performance tests](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/performance-tests/)

## 62. Outage testing

Do not claim that disk buffering works because it is enabled.

Test it.

1. Record counters.
2. Block all HEC destinations.
3. Generate numbered TCP events.
4. Confirm queue/disk growth.
5. Restart SC4S while Splunk remains unreachable.
6. Restore HEC.
7. Confirm the queue drains.
8. Count the numbered events in Splunk.
9. Check duplicates.
10. Check `dropped` delta.

For UDP sources, use a reliable test generator for the buffering test so sender-side UDP loss does not contaminate the result.

## 63. Security hardening

### Network

Permit only required flows:

```text
sources -> SC4S listener ports
SC4S -> HEC 8088
monitoring -> SC4S status if required
administration -> SSH
```

Do not expose generic syslog listeners to untrusted networks.

### TLS

Use TLS for:

- HEC;
- source syslog where the device supports it and operational constraints allow it.

### Secrets

Restrict `env_file`.

Rotate exposed tokens.

### Container

- pin an approved version/digest;
- avoid `latest` in controlled production;
- use least privilege consistent with required features;
- document when `--privileged` is introduced for eBPF.

### Host

- patch OS/runtime;
- use NTP;
- monitor disk;
- protect `/opt/sc4s`;
- audit service/config changes.

### Parser safety

Regex is executable workload.

A pathological pattern can become a denial-of-service vector at high EPS.

Keep filters narrow and benchmark them.

## 64. Change management

Treat SC4S metadata as production code.

Store sanitized configuration in Git:

```text
sc4s/
  env_file.example
  context/
    splunk_metadata.csv
    compliance_meta_by_source.conf
    compliance_meta_by_source.csv
  config/
    filters/
    log_paths/
  systemd/
    sc4s.service
  tests/
```

Never store real tokens.

For each change record:

- SC4S version;
- source vendor/model;
- firmware version;
- sample sanitized raw event;
- expected index;
- expected sourcetype;
- expected host;
- expected timestamp;
- expected fields;
- HEC indexes authorization;
- rollback.

## 65. Upgrade strategy

Before an SC4S upgrade:

1. Read release notes.
2. Record current image digest.
3. Save current local overrides.
4. Compare `splunk_metadata.csv.example`.
5. Check parser/source documentation for your vendors.
6. Test in non-production.
7. Re-run representative samples.
8. Re-run outage/buffer tests if the runtime/syslog engine changed.
9. Validate CPU and memory under load.
10. Keep the previous image locally for rollback in an air gap.

As of August 2026, the project is actively releasing SC4S 3.x. Do not assume parser internals are static.

## 66. Operational acceptance checklist

Before onboarding a source:

- index created;
- index authorized in HEC;
- HEC manual event succeeds;
- source documentation checked;
- correct protocol selected;
- correct listener active;
- metadata key verified against `.example`;
- raw event captured;
- sourcetype matches downstream TA;
- template preserves expected raw shape;
- timestamp verified;
- field extraction verified;
- HEC `dropped` counter not increasing;
- disk buffer capacity sufficient;
- monitoring alert configured.

Before declaring SC4S production-ready:

- service enabled at boot;
- offline image lifecycle tested where applicable;
- OS reboot tested;
- HEC outage tested;
- Splunk endpoint failover tested;
- disk-full warning threshold defined;
- source-side UDP loss monitored;
- configuration backed by source control;
- secrets excluded from source control;
- rollback documented.

## 67. When not to use SC4S

SC4S is not automatically the right answer for every input.

Do not force a source through SC4S when:

- the vendor has a robust native Splunk integration that already provides durable delivery;
- the source is not syslog-like and SC4S would only wrap/unwrap data pointlessly;
- a mandatory enterprise collector already provides equivalent parsing, durability, and Splunk metadata;
- you require durable raw-file retention as the primary system of record and a file-first syslog architecture is simpler;
- the environment cannot operate the container/networking requirements safely.

The goal is reliable observability, not architectural purity.

## 68. A practical decision matrix

| Requirement | SC4S | Plain syslog-ng | HF syslog input | syslog-ng + UF |
|---|---:|---:|---:|---:|
| Splunk-oriented vendor metadata | Strong | Build yourself | Build in Splunk | Split responsibility |
| HEC-native output | Strong | Possible | N/A as receiver | No |
| Persistent output buffer | Strong | Strong | Splunk queues | Strong via files |
| Source catalog | Strong | No Splunk-specific catalog | TAs/transforms | TAs/transforms |
| Arbitrary transformation | Strong but opinionated | Maximum | Strong | Strong |
| Raw file durability | Optional archive | Strong | Weak fit | Strong |
| Operational simplicity for many syslog vendors | Strong | Depends on expertise | Degrades at scale | Moderate |
| Air-gap operation | Yes with image lifecycle | Yes | Yes | Yes |
| High-EPS tuning | Strong controls | Maximum controls | Different model | Strong |
| Native Splunk S2S | No, uses HEC | No | Yes | UF yes |

## 69. A reference architecture for a mixed security estate

```text
                         Security / Network Sources
                 +--------------+--------------+
                 |              |              |
             FortiGate        Cisco           DLP
             UDP/TCP          TCP/TLS         TCP
                 \              |              /
                  \             |             /
                   +------------v------------+
                   |          SC4S           |
                   |                         |
                   | listeners               |
                   | source identification   |
                   | vendor parsers          |
                   | metadata overrides      |
                   | templates               |
                   | disk buffering          |
                   +------------+------------+
                                |
                                | HTTPS HEC
                                v
                       +--------+--------+
                       | HEC VIP/indexers|
                       +--------+--------+
                                |
                         Splunk indexers
                                |
                      +---------+---------+
                      |         |         |
                     fgt        av       dlp
                    index     index     index
```

The ports are transport decisions.

The indexes are metadata/governance decisions.

The HEC endpoint is the delivery mechanism.

SC4S connects those decisions without making them the same thing.

## 70. The operational lesson

The biggest SC4S mistakes are rarely syntax mistakes.

They are mental-model mistakes:

- assuming the HEC default index overrides event metadata;
- assuming a healthy process means healthy data;
- assuming TCP means no loss;
- assuming a disk buffer protects the sender-to-collector path;
- assuming an index name in SC4S is mandatory;
- assuming a dedicated port automatically identifies a vendor;
- assuming a message that looks like JSON somewhere in the path still reaches Splunk as pure JSON;
- assuming adding more collectors behind a load balancer automatically improves syslog availability.

Once you separate transport, framing, parsing, metadata, buffering, and destination authorization, SC4S becomes much easier to reason about.

That is the real value of the platform: not that it removes syslog engineering, but that it gives that engineering a consistent structure.

## 71. Command reference

### Service

```bash
systemctl status sc4s --no-pager -l
systemctl enable sc4s
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

### Statistics

```bash
docker exec SC4S syslog-ng-ctl stats
docker exec SC4S syslog-ng-ctl stats \
  | grep 'dst.http;d_hec_fmt'
```

### Ports

```bash
ss -lunp
ss -lntp
```

### Packets

```bash
tcpdump -ni any -s0 -A 'udp port 5514'
tcpdump -ni any -s0 -A 'tcp port 1514'
```

### UDP kernel health

```bash
netstat -su
```

### Effective SC4S environment

```bash
docker inspect SC4S \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sort
```

### Preprocessed syslog-ng config

```bash
docker exec SC4S \
  syslog-ng-ctl config --preprocessed
```

### HEC health

```bash
curl --cacert /opt/sc4s/tls/trusted.pem \
  https://splunk-hec.example.net:8088/services/collector/health
```

### Splunk HEC effective input

```bash
/opt/splunk/bin/splunk btool inputs list --debug
```

### SC4S event health search

```spl
index=main sourcetype="sc4s:events"
| sort - _time
```

### Source classification

```spl
index=* sc4s_fromhostip="<SOURCE_IP>"
| stats count by index sourcetype sc4s_vendor sc4s_product sc4s_proto
```

### Ingestion delay

```spl
index=<index>
| eval ingest_delay=_indextime-_time
| stats count avg(ingest_delay) max(ingest_delay) by sourcetype
```

### Downloadable, sanitized examples

These files use documentation-only addresses and deployment placeholders. Review every value against the SC4S release you run before using them:

- [SC4S `env_file` example](/examples/sc4s/env_file.example)
- [Splunk HEC `inputs.conf` example](/examples/sc4s/hec-inputs.conf.example)
- [SC4S metadata overrides](/examples/sc4s/splunk_metadata.csv)
- [Read-only troubleshooting script](/examples/sc4s/troubleshoot-sc4s.sh)
- [Offline systemd unit example](/examples/sc4s/sc4s.service.offline.example)
- [Optional macvlan example](/examples/sc4s/macvlan-example.sh)

## 72. Further reading

### SC4S

- [SC4S GitHub repository](https://github.com/splunk/splunk-connect-for-syslog)
- [SC4S documentation](https://splunk.github.io/splunk-connect-for-syslog/main/)
- [Architecture considerations](https://splunk.github.io/splunk-connect-for-syslog/main/architecture/)
- [Quickstart](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/quickstart_guide/)
- [Splunk setup for SC4S](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-splunk-setup/)
- [Runtime configuration](https://splunk.github.io/splunk-connect-for-syslog/main/gettingstarted/getting-started-runtime-configuration/)
- [Configuration reference](https://splunk.github.io/splunk-connect-for-syslog/main/configuration/)
- [Destinations](https://splunk.github.io/splunk-connect-for-syslog/main/destinations/)
- [SIMPLE sources](https://splunk.github.io/splunk-connect-for-syslog/main/sources/simple/)
- [CEF sources](https://splunk.github.io/splunk-connect-for-syslog/main/sources/base/cef/)
- [Fortinet FortiOS](https://splunk.github.io/splunk-connect-for-syslog/main/sources/vendor/Fortinet/fortios/)
- [Parser development](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/)
- [Filter development](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/filter_message/)
- [Parser message handling](https://splunk.github.io/splunk-connect-for-syslog/develop/creating_parsers/parse_message/)
- [Fine tuning](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/fine-tuning/)
- [Performance tests](https://splunk.github.io/splunk-connect-for-syslog/develop/architecture/performance-tests/)
- [Load balancer considerations](https://splunk.github.io/splunk-connect-for-syslog/latest/architecture/lb/)
- [SC4S releases](https://github.com/splunk/splunk-connect-for-syslog/releases)

### Syslog engine / AxoSyslog

- [Templates and macros](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/customizing-message-format/configuring-macros/)
- [Message manipulation](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/)
- [Rewrite rules](https://axoflow.com/docs/axosyslog-core/chapter-manipulating-messages/modifying-messages/)
- [Regex parser](https://axoflow.com/docs/axosyslog-core/chapter-parsers/parser-regexp/)
- [Syslog parsing](https://axoflow.com/docs/axosyslog-core/chapter-parsers/parser-syslog/)

### Docker networking

- [Docker host network](https://docs.docker.com/engine/network/drivers/host/)
- [Docker macvlan](https://docs.docker.com/engine/network/drivers/macvlan/)
- [Docker network drivers](https://docs.docker.com/engine/network/drivers/)

### Vendor integration

- [Bitdefender GravityZone Splunk integration](https://www.bitdefender.com/business/support/en/77212-171475-splunk.html)

### GitHub Pages

- [GitHub Pages site creation](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)

---

### Closing note

A collector should be uneventful during normal operation and precise when something fails.

The goal is not to create the most clever syslog configuration. It is to create a path where you can explain, with evidence, what happened to an event at every boundary: the sender, the network, the Linux socket, SC4S classification, metadata, queue, HEC request, Splunk index, and final field extraction.

In practice, I work through those boundaries in the same order used throughout this guide: packet, socket, parser, metadata, queue, HEC request, index, and fields. If I cannot show what happened at one of them, the pipeline is not ready for production.
