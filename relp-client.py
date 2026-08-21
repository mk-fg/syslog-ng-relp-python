## Can be either included or embedded into syslog-ng.conf via `python { ... }` block
## Code here should be exactly same as inside syslog-ng.relp-client.conf next to it

import os, sys, socket, select, time, enum, syslogng as sng

# Direct print+flush are used because syslog-ng Logger is more
#  difficult to configure (e.g. eats debug) and significatly delays messages.
# This should end up in journal, and eventually relayed if/when relp reconnects.
pr_debug = pr_err = lambda *a,**kw: print(*a, **kw, file=sys.stderr, flush=True)
err_fmt = lambda err: f'[{err.__class__.__name__}] {err}'

class adict(dict):
	def __init__(self, *args, **kws):
		super().__init__(*args, **kws)
		self.__dict__ = self

def str_cut(s, max_len, repr_fmt=True, ext='... {s_len}'):
	if isinstance(s, bytearray): s = bytes(s) # for nicer repr()
	if repr_fmt: s = repr(s)
	elif isinstance(s, bytes): s = s.decode(errors='replace')
	elif not isinstance(s, str): s = str(s)
	s = s.replace('\n', ' ⏎ ')
	s_len, ext_tpl = f'{len(s):,d}', ext.format(s_len='12/345')
	if max_len > 0 and len(s) > max_len:
		s_len = f'{max_len}/{s_len}'
		s = s[:max_len - len(ext_tpl)] + ext.format(s_len=s_len)
	return s


class TBFRateLimit(sng.LogParser):

	def token_bucket_iter(self, rate, rate_td=1, burst=1):
		rate /= rate_td; n, ts, d = max(0, burst - 1), time.monotonic(), (yield) or 1
		while True:
			n = min(burst, n - rate * (ts - (ts := time.monotonic())))
			n, d = (n - d, (yield) or 1) if n >= d else (n, (yield (d - n) / rate) or 1)

	def init(self, opts):
		n, td, burst = ( int(opts.get(k, 1))
			for k in 'tokens_ _within_seconds _with_burst'.split() )
		self.tbf_warned, self.tbf_repr = False, (
			f'{n:,d}t' + ('/s' if td == 1 else f'/{td:,d}s') + f':{burst:,d}'*(burst!=1) )
		self.tbf = self.token_bucket_iter(n, td, burst)
		return True

	def parse(self, msg, warn_len=100):
		if not (td := next(self.tbf)): self.tbf_warned = False
		else:
			if self.tbf_warned: return
			tbf_warn = f'[ sng-rate-limit {self.tbf_repr} active for {td:,.1f}s ]'
			if len(m := msg['MESSAGE'].decode()) >= warn_len - (wl := len(tbf_warn)):
				m = m[:warn_len - wl - 3] + ' …'
			msg['MESSAGE'] = f'{m} {tbf_warn}'; self.tbf_warned = True
		return True


class RELPDestination(sng.LogDestination):
	# syslog-ng needs e.g. TimeoutStopSec=5 with this,
	#  as otherwise it can hang on some blocking call here during shutdown.

	opts_default = dict( ipv4='', ipv6='', port=0,
		timeout=151, nak_max=3, stderr_prefix='RELP: ',
		reconn_min=5, reconn_max=3600, reconn_factor=1.3, log='info' )

	def init(self, opts):
		opts = adict(self.opts_default, **dict((k.replace(*'-_'), v) for k, v in opts.items()))
		self.ep = opts.get(k := 'ipv4') or opts.get(k := 'ipv6'), int(opts.port)
		if not all(self.ep): raise ValueError('Missing required ipv4/ipv6 or port option(s)')
		self.sock = self.sock_in = self.sock_out = None
		self.sock_af = socket.AF_INET if k == 'ipv4' else socket.AF_INET6
		self.sock_timeout, self.nak_max = float(opts.timeout), int(opts.nak_max)
		self.reconn = adict( ts=0, fails=0, td_warn=True,
			k=float(opts.reconn_factor), min=float(opts.reconn_min), max=float(opts.reconn_max) )
		try: log = enum.IntEnum('log', ll := 'quiet error info debug')[log := opts.log.strip().lower()]
		except KeyError: raise ValueError(f'"log" option value [ {log!r} ] must be one of: {ll}')
		log_null = lambda msg: None
		log_pre = ( lambda f,_pre=opts.stderr_prefix:
			log_null if log is log.quiet else (lambda msg: f(f'{_pre}{msg}')) )
		self.log_err, self.log_debug = (
			log_pre(pr_err), log_pre(pr_debug) if log is log.debug else log_null )
		self.log_init = log_null if log > log.info else self.log_err
		self.log_init(f'initialized with {self.ep} destination')
		return True

	def reconn_check(self):
		'Returns True for first reconn go-ahead, or ... for subsequent ones'
		first_retry, td_retry = not (rc := self.reconn).fails, rc.min * rc.k ** rc.fails
		if (td_conn := (ts := time.monotonic()) - rc.ts) < td_retry:
			if rc.td_warn: rc.td_warn = self.log_err( 'conn failed too'
				f' quickly ({td_conn:.1f}s), retry in >{td_retry - td_conn:.1f}s' )
			return
		rc.fails += 1; rc.ts = ts; return True if first_retry else ...

	relp_cmd = enum.Enum('RELPCmd', 'rsp open close serverclose syslog')

	def relp_err(self, msg):
		'Log error and close connection'
		self.log_err(msg); self.sock_close()

	def relp_read(self, ln=None, bs=5_000):
		if not (buff := (st := self.sock_st).get('buff')):
			st.n = 0; buff = st.buff = bytearray(bs)
		n, ts0, msg = st.n, time.monotonic(), None
		while True:
			if ln and n >= ln: # exact-length reads
				msg, nn, n = buff[:ln], ln, n - ln
				if msg[-1] != 0xa: return self.relp_err('msg-end-bug :: ' + str_cut(msg, 50))
			elif not ln and (nn := buff[:n].find(0xa) + 1): msg, n = buff[:nn], n - nn # readline
			if msg is not None:
				if n: buff[:n] = buff[nn:nn+n]
				st.n = n; return msg
			evs = self.sock_in.poll( maxevents=1,
				timeout=self.sock_timeout - (time.monotonic() - ts0) )
			if not (evs and evs[0][1] & select.EPOLLIN) or n == bs:
				return self.relp_err('recv-err :: ' + ('closed/error' if evs else 'timeout'))
			mv = memoryview(buff)[n:]
			try: nn = self.sock.recv_into(mv, bs-n)
			except BlockingIOError: continue
			except OSError as err: return self.relp_err(f'recv-err :: {err_fmt(err)}')
			n += nn; ts0 = time.monotonic()

	def relp_read_cmd(self, txnr_ack=None):
		'Returns RELP (cmd, msg, data) or True if txnr_ack == rsp.txnr or None'
		if not (msg_raw := self.relp_read()): return
		try:
			txnr, cmd, msg = msg_raw[:-1].split(b' ', 2)
			if len(msg) == 1 and msg[0] == 0x30: n, msg = 0, b''
			else: n, msg = msg.split(b' ', 1)
			txnr, n, data, cmd = int(txnr), int(n), b'', self.relp_cmd[cmd.decode()]
		except Exception as err: return self.relp_err(
			f'parse-err :: {err_fmt(err)} :: ' + str_cut(msg_raw, 50) )
		if len(msg) < n:
			if not (data := self.relp_read(n - len(msg))): return
		if (msg_n := len(msg) + len(data)) != n: return self.relp_err(
			f'msg-len-bug {msg_n} != {n} :: ' + str_cut(msg_raw, 50) )
		if cmd is [cmd.close, cmd.serverclose]: return self.relp_err(cmd.name)
		if not txnr_ack: return cmd, msg, data
		if cmd is not cmd.rsp or txnr != txnr_ack:
			return self.relp_err(f'noise :: {cmd.name} :: ' + str_cut(msg_raw, 50))
		rsp, _, err = msg.partition(b' ')
		if int(rsp) == 200: self.sock_st.n_nak = 0; return True
		self.sock_st.n_nak += 1 # reconnect on N NAKs in a row, with delay logic there
		(self.log_err if self.sock_st.n_nak <= self.nak_max else self.repl_err)(
			'NAK :: ' + str_cut((err + b'\n' + data).decode(errors='replace'), 80) )

	def relp_send(self, cmd, msg=b''):
		'Returns txnr of sent msg, or None on usual net issues'
		if not isinstance(cmd, str): cmd = cmd.name
		if isinstance(data := msg.rstrip(), str): data = data.encode()
		sent, txnr, n = False, self.sock_st.txnr, len(data)
		buff = bytearray(f'{txnr} {cmd} {n} '.encode())
		buff.resize((nn := len(buff)) + n + 1); buff[nn:nn+n] = data; buff[-1] = 0xa
		ts0, mv = time.monotonic(), memoryview(buff)
		while True:
			evs = self.sock_out.poll( maxevents=1,
				timeout=self.sock_timeout - (time.monotonic() - ts0) )
			if not (evs and evs[0][1] & select.EPOLLOUT):
				return self.relp_err('send-err :: ' + ('closed/error' if evs else 'timeout'))
			if not sent:
				self.sock_st.txnr += 1; sent = True
				if self.sock_st.txnr >= 1_000_000_000: self.sock_st.txnr = 1
			try: nn = self.sock.send(mv)
			except BlockingIOError: continue
			except OSError as err: return self.relp_err(f'send-err :: {err_fmt(err)}')
			if not (mv := mv[nn:]): return txnr

	def open(self):
		if self.sock: self.sock_close()
		if not (rc := self.reconn_check()): return False # rate-limits connection attempts
		fail = self.log_debug if rc is ... else self.relp_err # log first reconn attempt
		fail = lambda msg,_f=fail: _f(msg) or False
		s = self.sock = socket.socket(self.sock_af, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		self.sock_in = select.epoll(); self.sock_in.register(s, select.EPOLLIN)
		self.sock_out = select.epoll(); self.sock_out.register(s, select.EPOLLOUT)
		ts0, self.sock_st = time.monotonic(), adict(txnr=1, n_nak=0)
		self.log_debug(f'conn to {self.ep} (timeout={self.sock_timeout:,.1f}s)...')
		try: s.settimeout(self.sock_timeout); s.connect(self.ep)
		except OSError as err: self.sock_close(); return fail(
			f'conn failed (td={time.monotonic() - ts0:,.1f}s) :: {err_fmt(err)}' )
		try:
			txnr = self.relp_send( self.relp_cmd.open, 'relp_version=1\nrelp_software='
				'sng-relp,1,https://github.com/mk-fg/syslog-ng-relp-python\ncommands=syslog' )
			if not self.relp_read_cmd(txnr): return fail('handshake rejected')
		except OSError as err:
			self.sock_close(); return fail(f'handshake FAIL :: {err_fmt(err)}')
		self.reconn.update(fails=0, td_warn=True, ts=(ts := time.monotonic()))
		(self.log_init or self.log_debug)(f'connected to {self.ep} (td={ts - ts0:,.1f}s)')
		self.log_init = None; s.setblocking(False); return True

	def send(self, msg, msg_fmt='isodate loghost msghdr message'.upper().split()):
		# Strictly synchronous one-by-one send-and-wait like this is terrible with latencies
		# send() -> queued + flush() can be used for parallel send, but can only
		#  be acked all-or-nothing to sng, so can be tricky and desync state from there.
		# msg example: <133>2026-08-15T22:13:19.316127+05:00 hostname prog[123] msg...
		if not self.sock: return self.NOT_CONNECTED
		m = list(msg.get(k) for k in msg_fmt)
		try:
			m = f'<{int(msg["TAG"], 16)}>'.encode() + b' '.join(m[:-1]) + m[-1]
			txnr_ack = self.relp_send(self.relp_cmd.syslog, m.replace(b'\n', b' \xe2\x8f\x8e '))
			if not txnr_ack: return self.NOT_CONNECTED
		except Exception as err:
			self.relp_err(f'msg-send-err :: {err_fmt(err)} :: ' + str_cut(m, 150))
			return self.ERROR
		if not (ack := self.relp_read_cmd(txnr_ack)): return self.RETRY
		elif ack is True: return self.SUCCESS
		else: raise ValueError(f'BUG - relp_read_cmd return = {ack!r}')

	def sock_close(self):
		if self.sock_in: self.sock_in.close(); self.sock_in = None
		if self.sock_out: self.sock_out.close(); self.sock_out = None
		if self.sock:
			self.log_debug(f'conn close {self.ep}')
			try: self.sock.shutdown(socket.SHUT_RDWR)
			except OSError: pass
			self.sock.close(); self.sock = None

	def close(self):
		if self.sock and (txnr := self.relp_send(self.relp_cmd.close)) and (
			not (ack := self.relp_read_cmd()) or (cmd := ack[0])
			not in [cmd.rsp, cmd.serverclose] ): return self.relp_err('close-noack')
		self.sock_close()

	def is_opened(self): return bool(self.sock)
	def deinit(self): self.close()
