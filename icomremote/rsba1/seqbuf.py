"""Receive-side reorder buffer with retransmit requests.

Packets carry a 16-bit sequence number. The buffer releases them strictly in
order. When a gap appears it asks the sender to retransmit the missing range
(once) and holds later packets back for up to `hold` seconds (at least two
round trips). If the missing packets still haven't arrived, the gap is
declared lost and delivery resumes from the next packet held.

Pure logic, no I/O and no clock of its own: the caller passes `now`, which
makes it testable in simulated time. Same behaviour as kappanhang's seqbuf.go,
restructured around an explicit expected-sequence counter.
"""

MAX_SEQ = 0x10000
MAX_RETRANSMIT_SPAN = 10      # don't ask for more than this many packets at once
RESYNC_DISTANCE = 1000        # a jump this far ahead means the sender restarted
LATE_RESYNC = 20              # this many "already delivered" packets in a row: the
                              # sender's counter went back; follow it instead of
                              # dropping everything until it catches up (~11 min of audio)


def seq_diff(a, b):
    """Signed distance a - b on the 16-bit circle, in [-32768, 32767]."""
    d = (a - b) & 0xFFFF
    return d - MAX_SEQ if d >= 0x8000 else d


class ReorderBuffer:
    def __init__(self, hold=0.1):
        self.hold = hold
        self.rtt = 0.0
        self._pending = {}           # seq -> payload
        self._expected = None
        self._gap_since = None       # time the current gap was first seen
        self._requested = None       # (first, last) already requested for this gap
        self.lost = 0
        self.retransmit_requests = 0
        self.duplicates = 0
        self.resyncs = 0
        self._late_run = 0

    def reset(self):
        self.__init__(self.hold)

    @property
    def expected(self):
        return self._expected

    def add(self, seq, payload):
        """Store a received packet. Returns False if it was late or a duplicate."""
        seq &= 0xFFFF
        if self._expected is None:
            self._expected = seq
        d = seq_diff(seq, self._expected)
        if d < 0:
            self._late_run += 1
            if self._late_run < LATE_RESYNC:
                self.duplicates += 1      # already delivered or given up on
                return False
            self._resync(seq)             # counter went back: follow it
        elif d > RESYNC_DISTANCE:         # sender restarted its counter
            self._resync(seq)
        self._late_run = 0
        if seq in self._pending:
            self.duplicates += 1
            return False
        self._pending[seq] = payload
        return True

    def _resync(self, seq):
        self._pending.clear()
        self._expected = seq
        self._gap_since = None
        self._requested = None
        self._late_run = 0
        self.resyncs += 1

    def poll(self, now):
        """-> (ready, request, next_deadline)

        ready:         [(seq, payload), ...] in order, to deliver now
        request:       (first, last) inclusive range to ask the sender for, or None
        next_deadline: time at which poll() should be called again, or None
        """
        ready = []
        request = None
        while self._pending:
            if self._expected in self._pending:
                ready.append((self._expected, self._pending.pop(self._expected)))
                self._expected = (self._expected + 1) & 0xFFFF
                self._gap_since = None
                self._requested = None
                continue
            # Gap: the next expected packet is missing but later ones are here.
            first_after = min(self._pending, key=lambda s: seq_diff(s, self._expected))
            last_missing = (first_after - 1) & 0xFFFF
            if self._gap_since is None:
                self._gap_since = now
            span = seq_diff(last_missing, self._expected) + 1
            if self._requested is None:
                self._requested = (self._expected, last_missing)
                if span <= MAX_RETRANSMIT_SPAN:
                    request = self._requested
                    self.retransmit_requests += 1
            hold = max(self.hold, 2.0 * self.rtt)
            deadline = self._gap_since + hold
            if now < deadline:
                return ready, request, deadline
            # Give up on the gap.
            self.lost += span
            self._expected = first_after
            self._gap_since = None
            self._requested = None
        return ready, request, None
