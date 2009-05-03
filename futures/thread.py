#!/usr/bin/env python

import queue
import threading

FIRST_COMPLETED = 0
FIRST_EXCEPTION = 1
ALL_COMPLETED = 2
RETURN_IMMEDIATELY = 3

_PENDING = 0
_RUNNING = 1
_CANCELLED = 2
_FINISHED = 3

_STATE_TO_DESCRIPTION_MAP = {
    _PENDING: "pending",
    _RUNNING: "running",
    _CANCELLED: "cancelled",
    _FINISHED: "finished"
}

class CancelledException(Exception):
    pass

class TimeoutException(Exception):
    pass

class Future(object):
    def __init__(self):
        self._condition = threading.Condition()
        self._state = _PENDING
        self._result = None
        self._exception = None

    def __repr__(self):
        with self._condition:
            if self._state == _FINISHED:
                if self._exception:
                    return '<Future state=%s raised %s>' % (
                        _STATE_TO_DESCRIPTION_MAP[self._state],
                        self._exception.__class__.__name__)
                else:
                    return '<Future state=%s returned %s>' % (
                        _STATE_TO_DESCRIPTION_MAP[self._state],
                        self._result.__class__.__name__)
            return '<Future state=%s>' % _STATE_TO_DESCRIPTION_MAP[self._state]

    def cancel(self):
        with self._condition:
            if self._state in [_RUNNING, _FINISHED]:
                return False

            self._state = _CANCELLED
            return True

    def cancelled(self):
        with self._condition:
            return self._state == _CANCELLED

    def done(self):
        with self._condition:
            return self._state in [_CANCELLED, _FINISHED]

    def __get_result(self):
        if self._exception:
            raise self._exception
        else:
            return self._result

    def result(self, timeout=None):
        with self._condition:
            if self._state == _CANCELLED:
                raise CancelledException()
            elif self._state == _FINISHED:
                return self.__get_result()

            print('Waiting...')
            self._condition.wait(timeout)
            print('Post Waiting...')

            if self._state == _CANCELLED:
                raise CancelledException()
            elif self._state == _FINISHED:
                return self.__get_result()
            else:
                raise TimeoutException()

    def exception(self, timeout=None):
        with self._condition:
            if self._state == _CANCELLED:
                raise CancelledException()
            elif self._state == _FINISHED:
                return self._exception

            self._condition.wait(timeout)

            if self._state == _CANCELLED:
                raise CancelledException()
            elif self._state == _FINISHED:
                return self._exception
            else:
                raise TimeoutException()

class _NullWaitTracker(object):
    def add_result(self):
        pass

    def add_exception(self):
        pass

    def add_cancelled(self):
        pass

class _FirstCompletedWaitTracker(object):
    def __init__(self):
        self.event = threading.Event()

    def add_result(self):
        self.event.set()

    def add_exception(self):
        self.event.set()

    def add_cancelled(self):
        self.event.set()

class _AllCompletedWaitTracker(object):
    def __init__(self, pending_calls, stop_on_exception):
        self.event = threading.Event()
        self.pending_calls = pending_calls
        self.stop_on_exception = stop_on_exception

    def add_result(self):
        self.pending_calls -= 1
        if not self.pending_calls:
            self.event.set()

    def add_exception(self):
        self.add_result()
        if self.stop_on_exception:
            self.event.set()

    def add_cancelled(self):
        self.add_result()

class _ThreadEventSink(object):
    def __init__(self):
        self._condition = threading.Lock()
        self._waiters = []

    def add(self, e):
        self._waiters.append(e)

    def add_result(self):
        with self._condition:
            for waiter in self._waiters:
                waiter.add_result()

    def add_exception(self):
        with self._condition:
            for waiter in self._waiters:
                waiter.add_exception()

    def add_cancelled(self):
        with self._condition:
            for waiter in self._waiters:
                waiter.add_cancelled()

class FutureList(object):
    def __init__(self, futures, event_sink):
        self._futures = futures
        self._event_sink = event_sink

    def wait(self, timeout=None, run_until=ALL_COMPLETED):
        with self._event_sink._condition:
            print('WAIT 123')
            if all(f.done() for f in self):
                return
            print('WAIT 1234')

            if run_until == FIRST_COMPLETED:
                m = _FirstCompletedWaitTracker()
            elif run_until == FIRST_EXCEPTION:
                m = _AllCompletedWaitTracker(len(self), stop_on_exception=True)
            elif run_until == ALL_COMPLETED:
                m = _AllCompletedWaitTracker(len(self), stop_on_exception=False)
            elif run_until == RETURN_IMMEDIATELY:
                m = _NullWaitTracker()
            else:
                raise ValueError()

            self._event_sink.add(m)

        if run_until != RETURN_IMMEDIATELY:
            print('WAIT 12345', timeout)
            m.event.wait(timeout)

    def cancel(self, timeout=None):
        for f in self:
            f.cancel()
        self.wait(timeout=timeout, run_until=ALL_COMPLETED)
        if any(not f.done() for f in self):
            raise TimeoutException()

    def has_running_futures(self):
        return bool(self.running_futures())

    def has_cancelled_futures(self):
        return bool(self.cancelled_futures())

    def has_done_futures(self):
        return bool(self.done_futures())

    def has_successful_futures(self):
        return bool(self.successful_futures())

    def has_exception_futures(self):
        return bool(self.exception_futures())
    
    def running_futures(self):
        return [f for f in self if not f.done() and not f.cancelled()]
  
    def cancelled_futures(self):
        return [f for f in self if f.cancelled()]
  
    def done_futures(self):
        return [f for f in self if f.done()]

    def successful_futures(self):
        return [f for f in self
                if f.done() and not f.cancelled() and f.exception() is None]
  
    def exception_futures(self):
        return [f for f in self if f.done() and f.exception() is not None]
  
    def __getitem__(self, i):
        return self._futures[i]

    def __len__(self):
        return len(self._futures)

    def __iter__(self):
        return iter(self._futures)

    def __contains__(self, f):
        return f in self._futures

    def __repr__(self):
        return ('<FutureList #futures=%d '
                '[#success=%d #exception=%d #cancelled=%d]>' % (
                len(self),
                len(self.successful_futures()),
                len(self.exception_futures()),
                len(self.cancelled_futures())))

class _WorkItem(object):
    def __init__(self, call, future, completion_tracker):
        self.call = call
        self.future = future
        self.completion_tracker = completion_tracker

    def run(self):
        if self.future.cancelled():
            with self.future._condition:
                self.future._condition.notify_all()
            self.completion_tracker.add_cancelled()
            return

        self.future._state = _RUNNING
        try:
            r = self.call()
        except BaseException as e:
            with self.future._condition:
                self.future._exception = e
                self.future._state = _FINISHED
                self.future._condition.notify_all()
            self.completion_tracker.add_exception()
        else:
            with self.future._condition:
                self.future._result = r
                self.future._state = _FINISHED
                self.future._condition.notify_all()
            self.completion_tracker.add_result()

class ThreadPoolExecutor(object):
    def __init__(self, max_threads):
        self._max_threads = max_threads
        self._work_queue = queue.Queue()
        self._threads = set()
        self._shutdown = False
        self._lock = threading.Lock()

    def _worker(self):
        try:
            while True:
                try:
                    work_item = self._work_queue.get(block=True,
                                                    timeout=0.1)
                except queue.Empty:
                    if self._shutdown:
                        return
                else:
                    work_item.run()
        except BaseException as e:
            print('Out e:', e)

    def _adjust_thread_count(self):
        for _ in range(len(self._threads),
                       min(self._max_threads, self._work_queue.qsize())):
            print('Creating a thread')
            t = threading.Thread(target=self._worker)
            t.daemon = True
            t.start()
            self._threads.add(t)

    def run(self, calls, timeout=None, run_until=ALL_COMPLETED):
        with self._lock:
            if self._shutdown:
                raise RuntimeError()

            futures = []
            event_sink = _ThreadEventSink()
            for call in calls:
                f = Future()
                w = _WorkItem(call, f, event_sink)
                self._work_queue.put(w)
                futures.append(f)
    
            print('futures:', futures)
            self._adjust_thread_count()
            fl = FutureList(futures, event_sink)
            fl.wait(timeout=timeout, run_until=run_until)
            return fl

    def runXXX(self, calls, timeout=None):
        fs = self.run(calls, timeout, run_util=FIRST_EXCEPTION)

        if fs.has_exception_futures():
            raise fs.exception_futures()[0].exception()
        else:
            return [f.result() for f in fs]

    def shutdown(self):
        with self._lock:
            self._shutdown = True