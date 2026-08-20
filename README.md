syslog-ng python RELP client
============================

This is an all-in-one configuration file example for [syslog-ng]
(OSE "Open Source Edition", built with python support enabled),
using remote logging over [Reliable Event Logging Protocol (RELP)],
fully implemented in an embedded `python { ... }` block/class there.

[RELP] is a simple and efficient protocol for sending logs over network,
with acknowledgements from the receiving side to avoid loosing any of them.
Protocol was developed for [rsyslog] daemon, and can be useful to interoperate with it.

This implementation currently only has RELP client (protocol version 1)
for sending logs to remote server, but I'll probably need server-side
too eventually, so will likely add that later here as well.\
It does not support TLS wrapping, intended to only run over [WireGuard tunnels],
other similar [SD-WAN] layers, or physical local networks/segments
(and I'd recommend to avoid app-level security mechanisms like TLS
wrappers in general - if possible - for many good time-proven reasons,
use network-level secure tunnels/vlans/proxies instead).\
Code easily fits into syslog-ng.conf file, does not have any extra dependencies.

My use-case for this is gradually replacing rsyslog with syslog-ng,
while keeping remote logging mechanisms working between the two,
using python RELP destination alongside python filtering/processing code.

syslog-ng (OSE) does not have built-in RELP destination at the moment
(as of 4.11.0 and 2026-08-16, see [syslog-ng github issue-4312]),
but has [OTLP] protocol support, which can work in a similar way to RELP,
except with way more complicated [gRPC]/HTTP2-based internals,
which I'd rather avoid for complexity/overhead reasons in my setups.

[syslog-ng]: https://syslog-ng.github.io/
[Reliable Event Logging Protocol (RELP)]:
  https://en.wikipedia.org/wiki/Reliable_Event_Logging_Protocol
[RELP]: https://en.wikipedia.org/wiki/Reliable_Event_Logging_Protocol
[rsyslog]: https://www.rsyslog.com/
[WireGuard tunnels]: https://www.wireguard.com/
[SD-WAN]: https://en.wikipedia.org/wiki/SD-WAN
[syslog-ng github issue-4312]: https://github.com/syslog-ng/syslog-ng/issues/4312
[OTLP]: https://opentelemetry.io/docs/specs/otlp/
[gRPC]: https://en.wikipedia.org/wiki/GRPC

Sections below:

- [How to use](#hdr-how_to_use)
- [Test run](#hdr-test_run)
- [How it works](#hdr-how_it_works)
- [Links](#hdr-links)

This repository URLs:

- <https://github.com/mk-fg/syslog-ng-relp-python>
- <https://codeberg.org/mk-fg/syslog-ng-relp-python>
- <https://fraggod.net/code/git/syslog-ng-relp-python>


<a name=hdr-how_to_use></a>
## How to use

Grab [syslog-ng.relp-client.conf] file from the repository, and edit
["destination" block] at the bottom, that looks something like this:

```
destination d_relp { python(
  class(RELPDestination) options(ipv4 => 127.0.0.1, port => 11122)
  disk-buffer( capacity-bytes(2000000) reliable(yes)
    flow-control-window-bytes(200000) dir('/var/spool/sng/queue') )
  time-reopen(20) ); };
```

[syslog-ng.relp-client.conf]: syslog-ng.relp-client.conf
["destination" block]:
  https://syslog-ng.github.io/admin-guide/070_Destinations/README

Such block configures where logs will be sent to, as well as any extra parameters (via [options()]):

- `ipv4` - IPv4 address or hostname (that will be resolved to IPv4) of [RELP]
  server, where logs routed to this "destination" block will be sent to.

- `ipv6` - same as `ipv4` above, but IPv6 address/hostname.
  Either `ipv4` or `ipv6` is required, with only `ipv4` used if both are specified.

- `port` - TCP port to use for specified ipv4/ipv6 server address. Required.

- `timeout` (seconds, default = 151) - max timeout until
  RELP message acknowledgements and between TCP network operations
  (connection, sending/receiving bytes, etc), to wait before considering
  that connection dead/broken and reconnecting.

- `nak-max` (default = 3) - max attempts to send syslog message if RELP
  server on the other end replies with error response instead of acknowledgement
  (in case it might be temporary server-side load/storage problem), before reconnecting.
  Delay between retries is up to syslog-ng (destination returns RETRY code to it),
  and is typically quite small.

- `reconn-min` / `reconn-max` / `reconn-factor` (default is 5s to 1h, with factor=1.3) -
  delay until reconnection after failed connection attempt.
  It starts with `reconn-min` seconds, scales up by `reconn-factor` on each consecutive failure
  (up to `reconn-max` seconds), and resets on any successful connection (incl. RELP handshake).

- `stderr-prefix` (default = "RELP: ") - prefix for any error messages logged to
  syslog-ng's stderr stream. Normally there should only be output on network/protocol errors,
  excluding repeated connection attempts.

- `log` (quiet/error/info/debug, default = info) - enables extra logging for (re-)connection attempts.
  "info" (default) is same as "error", except logs where/when first connection is established,
  to e.g. confirm that configuration worked after restart.

Note that RELP code logs to stderr stream directly, instead of using syslog-ng's
[internal() logging source], as I found latter to be delayed, as well as more
difficult and error-prone to configure correctly.\
Replace `pr_debug` and `pr_err` calls to `print()` at the top of python code block
with `sng.Logger()` debug/error methods to use that internal-logger instead.

Make sure to also enable/confiure [disk-buffer()] for message delivery to be actually reliable,
as messages have to be stored/buffered somewhere during any kind of network/destination problems,
system or syslog-ng daemon restarts.

[options()]:
  https://syslog-ng.github.io/admin-guide/070_Destinations/200_Python/000_Python_destination_options#options
[internal() logging source]:
  https://syslog-ng.github.io/admin-guide/060_Sources/010_Internal/README
[disk-buffer()]:
  https://syslog-ng.github.io/admin-guide/070_Destinations/200_Python/000_Python_destination_options#disk-buffer


<a name=hdr-test_run></a>
## Test run

Here's basic `rsyslog.sink.conf` that can work as a local RELP sink,
printing received logs to stdout:

```
## rsyslogd -n -f <conf> -i /tmp/relp.sink.pid
$AbortOnUncleanConfig on
$ErrorMessagesToStderr on
module(load="imrelp")
input(type="imrelp" port="11122")
module(load="builtin:omfile")
action(type="omfile" file="/dev/stdout")
```

Run rsyslog with it via: `rsyslogd -n -f rsyslog.sink.conf -i /tmp/relp.sink.pid`\
(use `pkill -F /tmp/relp.sink.pid` to stop it later)

And then run syslog-ng as RELP sender/client, with some generated noise from
stdin as logging source, using `syslog-ng.relp-client.conf` from this repository:

```
% syslog-ng -F -f syslog-ng.relp-client.conf --persist-file /tmp/relp.client.state \
 < <(while :; do ((n++)); echo "Log line $n from $(date -Is)"; sleep 1; done)
```

(stop it with Ctrl-C in its console later)

Then as syslog-ng RELP destination connects, it should be clear from syslog-ng
console output (enabled in [syslog-ng.relp-client.conf] in the repo here), and rsyslogd
output should immediately start printing lines generated by while-loop in command above.

RELP protocol is ASCII/plaintext, so can be easy to debug with common
[tcpdump]/[wireshark] network tools.

There are also tools like [flog] to generate more realistic-looking log lines for testing.

[tcpdump]: https://www.tcpdump.org/
[wireshark]: https://www.wireshark.org/
[flog]: https://github.com/mingrammer/flog


<a name=hdr-how_it_works></a>
## How it works

syslog-ng runs python destination in its own separate thread.

There it calls init(), open() and send() methods on specified destination class,
which all do blocking connect/send/recv operations, and return appropriate result code
to syslog-ng (e.g. SUCCESS if all's well, or RETRY, NOT_CONNECTED, ERROR, etc
on various issues, for syslog-ng to react accordingly).

[disk-buffer()] in ["destination" block] will store messages that were not yet
sent/acknowledged by RELP server, and RELPDestination.send() only returns SUCCESS code -
meaning that message can be removed from that buffer - when it gets an ack,
otherwise RETRY on nak, or ERROR/NOT_CONNECTED for network issues.

RELP messages are NOT batched or sent in parallel to each other (setting batch-\*
options will do nothing), as implementation here is simple and synchronous.

> It's likely possible using batch-\* options to queue and send/retry messages
> in parallel within such batches, only returning SUCCESS on batch flush() when all
> messages from it were delivered/acked in the background, but again, not implemented here.

This can be a major bottleneck in high-traffic scenarios with high network latency,
as sending each message requires full network round-trip, incl. time for message
packet(s) to arrive to destination and time to get ACK back from RELP server.\
So for example with network latency of 50ms, sending each message will take 100ms+,
and if latency jumps up to 80ms, then it'll be 160ms+/message, i.e. only about 6 msgs/s.

So in any kind of highload scenario, I'd recommend looking at
[syslog-ng OpenTelemetry transport] instead - it likely submits/acks messages in parallel,
and has [workers() option] to also use multiple connections as well, at the cost of
extra complexity and traffic overheads.

When stopping syslog-ng with python destination like this running, it will wait
for any blocking operations there to finish, which can take a while (up to configured
"timeout" parameter) if network connection or send/recv operation is in progress while
network itself is being disabled (e.g. on system shutdown).\
That's also potentially fixable with current syslog-ng implementation (by falsely
returning RETRY/NOT_CONNECTED statuses to it while doing blocking work in the background),
but not done here - use either shorter network timeout option or e.g. `TimeoutStopSec=5`
in syslog-ng [systemd service file] as a workaround.

[syslog-ng OpenTelemetry transport]:
  https://syslog-ng.github.io/admin-guide/070_Destinations/157_OpenTelemetry/README
[workers() option]:
  https://syslog-ng.github.io/admin-guide/070_Destinations/157_OpenTelemetry/000_opentelemetry-destination-options#workers
[systemd service file]: https://man.archlinux.org/man/systemd.service.5


<a name=hdr-links></a>
## Links

- [syslog-ng] - to load config/code here. I've only ever used OSE ("Open Source Edition") version.

- [Reliable Event Logging Protocol spec] - as HTML in [librelp github repo],
  download to .html file and Ctrl-O open in browser to easily read it.

- [librelp] - original C library implementing RELP protocol, written for [rsyslog].

- [syslog-ng github issue-4312] - "Add a reliable transport mode (eg the syslog-ng
  PE ALTP, or rsyslog RELP)" - where progress on built-in RELP implementation can
  be tracked/reported, if any.

- [syslog-ng OpenTelemetry transport] - [OTLP protocol] can be used as a built-in
  and more performant alternative to RELP, as well as maybe other protocols supported
  in non-OSE syslog-ng releases.

- [cybericius/syslog-ng-relp] - different third-party syslog-ng RELP implementation in [Go],
  to run as separate binary via program() destination pipe.

    Looks vibecoded and fundamentally broken, in that it doesn't confirm delivery to
    syslog-ng itself, so if e.g. network or RELP destination isn't working right,
    or syslog-ng is stopped/restarted, any messages in-flight, in socket and program()'s stdin
    pipe buffers will be lost (typically 64 KiB - check `echo | pipesz --get` on a specific system).
    Should work same as using TCP syslog socket (or likely worse, as socket will block earlier).

[Reliable Event Logging Protocol spec]: https://github.com/rsyslog/librelp/blob/master/doc/relp.html
[librelp github repo]: https://github.com/rsyslog/librelp/
[librelp]: https://www.rsyslog.com/librelp/
[OTLP protocol]: https://opentelemetry.io/docs/specs/otlp/
[cybericius/syslog-ng-relp]: https://github.com/cybericius/syslog-ng-relp
[Go]: https://go.dev/
