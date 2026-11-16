# RouterOS gateway template

The **device-independent baseline** every site gateway (`<site>-gw`) should carry: identity, remote
syslog, SNMP, NTP, DNS, management services, and the core OSPF plumbing. Distil your own from the
`/export` backups obi keeps (feature 026, `obi configs`). Site-specific interfaces, VLANs, bridges,
firewall, NAT, DHCP and BGP are **out of scope** here — this is only the part that should look the
same everywhere.

## Per-gateway parameters

Everything below is templated on three values. `{{id}}` is the site ID (the 3rd octet of the site's
management `/24`); `{{router-id}}` is the gateway's management address `.1`; `{{collector}}` is the
syslog/DNS host nearest the site. An example plan, for the fictional Example Valley Net
(`192.0.2.0/24` stands in for a private management range):

| Site | `{{id}}` | `{{router-id}}` | `{{collector}}` (syslog) |
|---|---:|---|---|
| core   | 0 | 192.0.2.1  | *is a collector* (192.0.2.101) |
| aspen  | 2 | 192.0.2.21 | 192.0.2.101 |
| birch  | 5 | 192.0.2.51 | 192.0.2.151 (second collector) |
| cedar  | 7 | 192.0.2.71 | 192.0.2.101 |

With two collectors, sites reachable through the second one use it; everyone else uses core.

## Baseline `.rsc`

Paste-able with the three placeholders substituted. Order follows `/export`; each block notes *why*.

```rsc
# --- identity & time -------------------------------------------------------
/system identity set name={{site}}-gw
/system clock set time-zone-name=<your IANA zone>
# NTP: be a server for the site, sync up to the pool
/system ntp client set enabled=yes
/system ntp server set enabled=yes use-local-clock=yes
/system ntp client servers add address=0.us.pool.ntp.org
/system ntp client servers add address=1.us.pool.ntp.org
/system ntp client servers add address=2.us.pool.ntp.org
/system ntp client servers add address=3.us.pool.ntp.org

# --- remote syslog (feeds obi's collector) ---------------------------------
# action 3 is the built-in "remote"; point it at the nearest collector.
/system logging action set 3 remote={{collector}} src-address={{router-id}}
/system logging add action=remote topics=info
/system logging add action=remote topics=warning
/system logging add action=remote topics=error
# keep a local disk copy too
/system logging action set 1 disk-file-count=16
/system logging add action=disk topics=info
/system logging add action=disk topics=warning
/system logging add action=disk topics=error

# --- SNMP (read-only, for obi metrics) -------------------------------------
/snmp community set [ find default=yes ] name=<COMMUNITY>
/snmp set enabled=yes

# --- DNS (resolver for site clients) ---------------------------------------
/ip dns set allow-remote-requests=yes servers=<collector-1>,<collector-2>

# --- management surface -----------------------------------------------------
/ip service set telnet disabled=yes
/ip service set ftp disabled=yes
/ip ssh set forwarding-enabled=both password-authentication=yes

# --- firmware ---------------------------------------------------------------
/system routerboard settings set auto-upgrade=yes
/system package update set channel=long-term

# --- OSPF (single flat area, hub at core) ----------------------------------
/routing ospf instance add name=ospf-instance-1 disabled=no \
    router-id={{router-id}} in-filter-chain=ospf-in redistribute=""
/routing ospf area add name=ospf-area-1 instance=ospf-instance-1 disabled=no
# standard in-filter: drop link-local, stamp our router-id as pref-src, accept
/routing filter rule add chain=ospf-in disabled=no \
    rule="if (dst in 169.254.0.0/16) { set comment \"rejected by ospf-in\"; reject }"
/routing filter rule add chain=ospf-in disabled=no rule="set pref-src {{router-id}}"
/routing filter rule add chain=ospf-in disabled=no rule=accept
# per-link: point-to-point peers get type=ptp + BFD + a cost; local nets are passive
#   /routing ospf interface-template add area=ospf-area-1 interfaces=<peer-vlan> type=ptp use-bfd=yes cost=<metric>
#   /routing ospf interface-template add area=ospf-area-1 interfaces=<mgmt-vlan> passive

# --- tools ------------------------------------------------------------------
/tool bandwidth-server set authenticate=no
/tool netwatch add name={{site}}-google     host=8.8.8.8 type=simple
/tool netwatch add name={{site}}-cloudflare host=1.1.1.1 type=simple
```

Notes:

- **`router-id`** may be a literal (`router-id={{router-id}}`) or a named `/routing id` object
  (`/routing id add id={{router-id}} name=id-1` then `router-id=id-1`). Both are in use; prefer the
  named form on new builds for readability.
- **Internet-edge gateways** additionally run **BGP** (your AS; `AS64500` is the documentation ASN)
  and an `ospf-out` filter chain that originates a default route (`originate-default=if-installed`).
  Newer edge configs name the chains `ospf-in-filter-chain` / `ospf-out-filter-chain`. Interior
  gateways don't originate default and only need `ospf-in`.
- **SNMP community** is redacted in the backups — substitute the real read-only community.

## Conformance

Audit live gateways against this baseline from the backups, e.g.:

```sh
cd ~/.local/state/obi/config-repo
for f in *-gw.rsc; do echo "== ${f%.rsc} =="; grep -hE "^/system logging" "$f"; done
```

Syslog gaps directly cause obi log blind spots, so remote logging is the first thing to check.
