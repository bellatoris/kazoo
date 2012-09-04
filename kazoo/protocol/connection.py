"""Zookeeper Protocol Connection Handler"""
import errno
import logging
import socket
from contextlib import contextmanager

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionDropped,
    EXCEPTIONS,
    SessionExpiredError,
    NoNodeError
)
from kazoo.protocol.serialization import (
    Auth,
    Close,
    Connect,
    Exists,
    GetChildren,
    Ping,
    ReplyHeader,
    Watch,
    int_struct
)
from kazoo.protocol.states import (
    Callback,
    KeeperState,
    WatchedEvent,
    EVENT_TYPE_MAP,
)

log = logging.getLogger(__name__)


CREATED_EVENT = 1
DELETED_EVENT = 2
CHANGED_EVENT = 3
CHILD_EVENT = 4

WATCH_XID = -1
PING_XID = -2
AUTH_XID = -4


@contextmanager
def socket_error_handling():
    try:
        yield
    except socket.error as e:
        if isinstance(e.args, tuple):
            raise ConnectionDropped("socket connection error: %s",
                                    errno.errorcode[e[0]])
        else:
            raise ConnectionDropped("socket connection error: %s", e)


class ConnectionHandler(object):
    """Zookeeper connection handler"""
    def __init__(self, client, retry_sleeper, log_debug=False):
        self.client = client
        self.handler = client.handler
        self.retry_sleeper = retry_sleeper

        # Our event objects
        self.reader_started = client.handler.event_object()
        self.reader_done = client.handler.event_object()
        self.writer_stopped = client.handler.event_object()
        self.writer_stopped.set()

        self.log_debug = log_debug
        self._socket = None
        self._xid = None

    def start(self):
        """Start the connection up"""
        self.handler.spawn(self.writer)

    def stop(self, timeout=None):
        """Ensure the writer has stopped, wait to see if it does"""
        self.writer_stopped.wait(timeout)
        return self.writer_stopped.is_set()

    def _read_header(self, timeout):
        b = self._read(4, timeout)
        length = int_struct.unpack(b)[0]
        b = self._read(length, timeout)
        header, offset = ReplyHeader.deserialize(b, 0)
        return header, b, offset

    def _read(self, length, timeout):
        msgparts = []
        remaining = length
        with socket_error_handling():
            while remaining > 0:
                s = self.handler.select([self._socket], [], [], timeout)[0]
                chunk = s[0].recv(remaining)
                if chunk == '':
                    raise ConnectionDropped('socket connection broken')
                msgparts.append(chunk)
                remaining -= len(chunk)
            return b"".join(msgparts)

    def _invoke(self, timeout, request, xid=None):
        """A special writer used during connection establishment only"""
        b = bytearray()
        if xid:
            b.extend(int_struct.pack(xid))
        if request.type:
            b.extend(int_struct.pack(request.type))
        b.extend(request.serialize())
        self._write(int_struct.pack(len(b)) + b, timeout)

        zxid = None
        if xid:
            header, buffer, offset = self._read_header(timeout)
            if header.xid != xid:
                raise RuntimeError('xids do not match, expected %r received %r',
                                   xid, header.xid)
            if header.zxid > 0:
                zxid = header.zxid
            if header.err:
                callback_exception = EXCEPTIONS[header.err]()
                log.debug('Received error %r', callback_exception)
                raise callback_exception
            return zxid

        msg = self._read(4, timeout)
        length = int_struct.unpack(msg)[0]
        msg = self._read(length, timeout)

        if hasattr(request, 'deserialize'):
            obj, _ = request.deserialize(msg, 0)
            log.debug('Read response %s', obj)
            return obj, zxid

        return zxid

    def _submit(self, request, timeout, xid=None):
        """Submit a request object with a timeout value and optional xid"""
        b = bytearray()
        b.extend(int_struct.pack(xid))
        if request.type:
            b.extend(int_struct.pack(request.type))
        b += request.serialize()
        self._write(int_struct.pack(len(b)) + b, timeout)

    def _write(self, msg, timeout):
        """Write a raw msg to the socket"""
        sent = 0
        msg_length = len(msg)
        with socket_error_handling():
            while sent < msg_length:
                s = self.handler.select([], [self._socket], [], timeout)[1]
                msg_slice = buffer(msg, sent)
                bytes_sent = s[0].send(msg_slice)
                if not bytes_sent:
                    raise ConnectionDropped('socket connection broken')
                sent += bytes_sent

    def _read_watch_event(self, buffer, offset):
        client = self.client
        watch, offset = Watch.deserialize(buffer, offset)
        path = watch.path

        if self.log_debug:
            log.debug('Received EVENT: %s', watch)

        watchers = set()
        with client._state_lock:
            # Ignore watches if we've been stopped
            if client._stopped.is_set():
                return

            if watch.type in (CREATED_EVENT, CHANGED_EVENT):
                watchers |= self.client._data_watchers.pop(path, set())
            elif watch.type == DELETED_EVENT:
                watchers |= self.client._data_watchers.pop(path, set())
                watchers |= self.client._child_watchers.pop(path, set())
            elif watch.type == CHILD_EVENT:
                watchers |= self.client._child_watchers.pop(path, set())
            else:
                log.warn('Received unknown event %r', watch.type)
                return

            # Strip the chroot if needed
            if self.client.chroot:
                path = path[len(self.client.chroot):]

        ev = WatchedEvent(EVENT_TYPE_MAP[watch.type], client._state, path)

        # Dump the watchers to the watch thread
        for watch in watchers:
            client.handler.dispatch_callback(Callback('watch', watch, (ev,)))

    def _read_response(self, header, buffer, offset):
        client = self.client
        request, async_object, xid = client._pending.get()
        if header.zxid and header.zxid > 0:
            client.last_zxid = header.zxid
        if header.xid != xid:
            raise RuntimeError('xids do not match, expected %r '
                               'received %r', xid, header.xid)

        # Determine if its an exists request and a no node error
        exists_request = isinstance(request, Exists)
        exists_error = header.err == NoNodeError.code and exists_request

        # Set the exception if its not an exists error
        if header.err and not exists_error:
            callback_exception = EXCEPTIONS[header.err]()
            if self.log_debug:
                log.debug('Received error %r', callback_exception)
            if async_object:
                async_object.set_exception(callback_exception)
        elif request and async_object:
            if exists_error:
                # It's a NoNodeError, which is fine for an exists
                # request
                async_object.set(None)
            else:
                try:
                    response = request.deserialize(buffer, offset)
                except Exception as exc:
                    if self.log_debug:
                        log.debug("Exception raised during deserialization"
                                  " of request: %s", request)
                    log.exception(exc)
                    async_object.set_exception(exc)
                    return
                log.debug('Received response: %r', response)
                async_object.set(response)

            # Determine if watchers should be registered
            watcher = getattr(request, 'watcher', None)
            with client._state_lock:
                if not client._stopped.is_set() and watcher:
                    if isinstance(request, GetChildren):
                        client._child_watchers[request.path].add(watcher)
                    else:
                        client._data_watchers[request.path].add(watcher)

        if isinstance(request, Close):
            if self.log_debug:
                log.debug('Read close response')
            self._socket.close()
            return True

    def reader(self, read_timeout):
        """Main reader function to read off the ZK connection"""
        self.reader_started.set()
        client = self.client
        if self.log_debug:
            log.debug("Reader started")

        while True:
            try:
                header, buffer, offset = self._read_header(read_timeout)
                if header.xid == PING_XID:
                    if self.log_debug:
                        log.debug('Received PING')
                elif header.xid == AUTH_XID:
                    if self.log_debug:
                        log.debug('Received AUTH')

                    if header.err:
                        # We go ahead and fail out the connection, mainly because
                        # thats what Zookeeper client docs think is appropriate

                        # XXX TODO: Should we fail out? Or handle auth failure
                        # differently here since the session id is actually valid!
                        with client._state_lock:
                            client._session_callback(KeeperState.AUTH_FAILED)
                        self.reader_done.set()
                        break
                elif header.xid == WATCH_XID:
                    self._read_watch_event(buffer, offset)
                else:
                    if self.log_debug:
                        log.debug('Reading for header %r', header)

                    if self._read_response(header, buffer, offset):
                        # _process_response returns True if the response
                        # indicated we should cease
                        break
            except ConnectionDropped as e:
                if self.log_debug:
                    log.debug('Connection dropped for reader: %s', e)
                break
            except Exception as e:
                log.exception(e)
                break

        self.reader_done.set()

        if self.log_debug:
            log.debug('Reader stopped')

    def writer(self):
        """Main writer function that writes to the ZK connection and handles
        other state management"""
        if self.log_debug:
            log.debug('Writer started')

        self.writer_stopped.clear()

        retry = self.retry_sleeper.copy()
        while not self.client._stopped.is_set():
            # If the connect_loop returns False, stop retrying
            if self._connect_loop(retry) is False:
                break

            # Still going, increment our retry then go through the
            # list of hosts again
            if not self.client._stopped.is_set():
                retry.increment()

        self.writer_stopped.set()
        if self.log_debug:
            log.debug('Writer stopped')

    def _connect_loop(self, retry):
        client = self.client
        writer_done = False
        for host, port in client.hosts:
            self._socket = self.handler.socket()

            if client._state != KeeperState.CONNECTING:
                with client._state_lock:
                    client._session_callback(KeeperState.CONNECTING)

            try:
                read_timeout, connect_timeout = self._connect(host, port)

                # Now that connection is good, reset the retries
                retry.reset()

                # Reset the reader events and spin it up
                self.reader_started.clear()
                self.reader_done.clear()
                self.handler.spawn(self.reader, read_timeout)
                self.reader_started.wait()
                self._xid = 0

                while not writer_done:
                    writer_done = self._send_request(read_timeout,
                                                     connect_timeout)

                if self.log_debug:
                    log.debug('Waiting for reader to read close response')
                self.reader_done.wait()
                if self.log_debug:
                    log.info('Closing connection to %s:%s', host, port)

                if writer_done:
                    with client._state_lock:
                        client._session_callback(KeeperState.CLOSED)
                    return False
            except ConnectionDropped:
                log.warning('Connection dropped')
                if client._state != KeeperState.CONNECTING:
                    with client._state_lock:
                        client._session_callback(KeeperState.CONNECTING)
            except AuthFailedError:
                log.warning('AUTH_FAILED closing')
                with client._state_lock:
                    client._session_callback(KeeperState.AUTH_FAILED)
                return False
            except SessionExpiredError:
                log.warning('Session has expired')
                with client._state_lock:
                    client._session_callback(KeeperState.EXPIRED_SESSION)
            except Exception as e:
                log.exception(e)
                raise
            finally:
                if not writer_done:
                    # The read thread will close the socket since there
                    # could be a number of pending requests whose response
                    # still needs to be read from the socket.
                    self._socket.close()

    def _connect(self, host, port):
        client = self.client
        log.info('Connecting to %s:%s', host, port)
        log.debug('    Using session_id: %r session_passwd: 0x%s',
                  client._session_id,
                  client._session_passwd.encode('hex'))

        try:
            self._socket.connect((host, port))
        except socket.error, e:
            if isinstance(e.args, tuple):
                raise ConnectionDropped("socket connection error: %s",
                                        errno.errorcode[e[0]])
            else:
                raise

        self._socket.setblocking(0)

        connect = Connect(0, client.last_zxid, client._session_timeout,
                          client._session_id or 0, client._session_passwd,
                          client.read_only)

        connect_result, zxid = self._invoke(client._session_timeout, connect)

        if connect_result.time_out <= 0:
            raise SessionExpiredError("Session has expired")

        if zxid:
            client.last_zxid = zxid

        # Load return values
        client._session_id = connect_result.session_id
        negotiated_session_timeout = connect_result.time_out
        connect_timeout = negotiated_session_timeout / len(client.hosts)
        read_timeout = negotiated_session_timeout * 2.0 / 3.0
        client._session_passwd = connect_result.passwd
        log.debug('Session created, session_id: %r session_passwd: 0x%s\n'
                  '    negotiated session timeout: %s\n'
                  '    connect timeout: %s\n'
                  '    read timeout: %s', client._session_id,
                  client._session_passwd.encode('hex'), negotiated_session_timeout,
                  connect_timeout, read_timeout)

        client._session_callback(KeeperState.CONNECTED)

        for scheme, auth in client.auth_data:
            ap = Auth(0, scheme, auth)
            zxid = self._invoke(connect_timeout, ap, xid=-4)
            if zxid:
                client.last_zxid = zxid
        return read_timeout, connect_timeout

    def _send_request(self, read_timeout, connect_timeout):
        client = self.client
        ret = None
        try:
            request, async_object = client._queue.peek(True,
                read_timeout / 2000.0)

            # Special case for auth packets
            if request.type == Auth.type:
                with client._state_lock:
                    self._submit(request, connect_timeout, AUTH_XID)
                    client._queue.get()
                return

            self._xid += 1

            if self.log_debug:
                log.debug('xid: %r', self._xid)

            with client._state_lock:
                self._submit(request, connect_timeout, self._xid)

                if isinstance(request, Close):
                    if self.log_debug:
                        log.debug('Received close req, closing')
                    ret = True

                client._queue.get()
                client._pending.put((request, async_object, self._xid))
        except self.handler.empty:
            if self.log_debug:
                log.debug('Queue timeout.  Sending PING')
            self._submit(Ping, connect_timeout, PING_XID)
        except Exception as e:
            log.exception(e)
            ret = True
        return ret