# CustomCloud.py - Hologram Python SDK Custom Cloud interface
#
# Author: Hologram <support@hologram.io>
#
# Copyright 2016 - Hologram (Konekt, Inc.)
#
#
# LICENSE: Distributed under the terms of the MIT License

from collections import deque
import socket
import sys
import threading
import time
from select import select
from Hologram.Cloud import Cloud
from Exceptions.HologramError import HologramError

MAX_RECEIVE_BYTES = 1024
MAX_QUEUED_CONNECTIONS = 5
RECEIVE_TIMEOUT = 5
SEND_TIMEOUT = 5
MIN_PERIODIC_INTERVAL = 1

def recvall(s, timeout=None, encoding=None):
    start_time = time.time()
    s.setblocking(0)
    timeleft = max(0, timeout or 1)
    recv = b''
    while True:
        rlist, _, xlist = select([s], [], [s], min(1, timeleft))
        if s in rlist:
            result = s.recv(MAX_RECEIVE_BYTES)
            if not result:
                break
            recv += result
        if s in xlist:
            break
        if timeout is None:
            continue
        timeleft = timeout - (time.time() - start_time)
        if timeleft <= 0:
            break
    if encoding:
        recv = recv.decode(encoding)
    return recv

class CustomCloud(Cloud):

    def __init__(self, credentials, send_host='', send_port=0,
                 receive_host='', receive_port=0, enable_inbound=False,
                 network=''):

        super().__init__(credentials,
                         send_host=send_host,
                         send_port=send_port,
                         receive_host=receive_host,
                         receive_port=receive_port,
                         network=network)

        # Enforce that the send and receive configs are set before using the class.
        if enable_inbound and (receive_host == '' or receive_port == 0):
            raise HologramError('Must set receive host and port for inbound connection')

        self._periodic_msg = None
        # We start with the event set, clear it when running and then set when
        # shutting down. This way, the thread can wait on it and stop immediately
        # when the script is exiting
        self._periodic_msg_disabled = threading.Event()
        self._periodic_msg_disabled.set()

        self._receive_buffer_lock = threading.Lock()
        self._receive_cv = threading.Lock()
        self._receive_buffer = deque()
        self._receive_socket = None

        self._accept_thread = None
        self.socketClose = True
        self._is_send_socket_open = False

        if enable_inbound:
            self.initializeReceiveSocket()

    def is_ready_to_send(self):
        return self.network is None or self.network.is_connected()

    # EFFECTS: Sends the message to the cloud.
    def sendMessage(self, message, timeout=SEND_TIMEOUT, close_socket=True):

        try:
            if not self.is_ready_to_send():
                self.addPayloadToBuffer(message)
                return ''

            self.open_send_socket(timeout=timeout)

            self.logger.info("Sending message with body of length %d", len(message))
            self.logger.debug('Send: %s', message)

            resultbuf = ''
            if self.__to_use_at_sockets():
                resultbuf = self.network.send_message(message)
            else:
                self.sock.send(message)
                resultbuf = self.receive_send_socket()

            self.logger.info('Sent.')

            if close_socket:
                self.close_send_socket()

            self.event.broadcast('message.sent')
            return resultbuf
        except IOError:
            self.__enforce_network_disconnected()
            self.logger.error('An error occurred while attempting to send the message to the cloud')
            self.logger.error('Please try again.')
            return ''
        except Exception as e:
            self.__enforce_network_disconnected()
            raise


    def open_send_socket(self, timeout=SEND_TIMEOUT):

        if self._is_send_socket_open:
            return

        self.__enforce_send_host_and_port()
        self.logger.info("Connecting to: %s", self.send_host)
        self.logger.info("Port: %s", self.send_port)

        # Check if we're going to use the AT command version of sockets or the
        # native Python socket lib.
        if self.__to_use_at_sockets():
            self.network.create_socket()
            self.network.connect_socket(self.send_host, self.send_port)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((self.send_host, self.send_port))

        self._is_send_socket_open = True


    def close_send_socket(self):
        try:
            # Check if we're going to use the AT command version of sockets or the
            # native Python socket lib.
            if self.__to_use_at_sockets():
                self.network.close_socket()
            else:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except socket.error:
                    pass

                self.sock.close()

            self._is_send_socket_open = False
            self.logger.info('Socket closed.')
        except IOError:
            self.logger.error('An error occurred while attempting to send the message to the cloud')
            self.logger.error('Please try again.')

    # EFFECTS: Receives data from inbound socket.
    def receive_send_socket(self, max_receive_bytes=MAX_RECEIVE_BYTES):
        resultbuf = b''
        while max_receive_bytes > 0:
            try:
                result = self.sock.recv(max_receive_bytes)
            except socket.timeout:
                break
            if not result:
                break
            resultbuf += result
            max_receive_bytes -= len(result)
        return resultbuf

    # REQUIRES: The interval in seconds, message body, optional topic(s) and a timeout value
    #           for how long in seconds before the socket is closed.
    # EFFECTS: Sends asychronous periodic messages every interval seconds.
    def sendPeriodicMessage(self, interval, message, topics=None, timeout=5):

        try:
            self._enforce_minimum_periodic_interval(interval)

            if not self._periodic_msg_disabled.is_set():
                raise HologramError('Cannot have more than 1 periodic message job at once')
            self._periodic_msg_disabled.clear()

        except Exception as e:
            self.__enforce_network_disconnected()
            raise

        self._periodic_msg = threading.Thread(target=self._periodic_job_thread,
                                              args=[interval, self.sendMessage,
                                                    message, topics, timeout])
        self._periodic_msg.start()

    def sendSMS(self, destination_number, message):
        raise NotImplementedError('Cannot send SMS via custom cloud')


    def __to_use_at_sockets(self):
        return self.network is not None and self.network.at_sockets_available

    # EFFECTS: This threaded infinite loop shoud keep sending messages with the specified
    #          interval.
    def _periodic_job_thread(self, interval, function, *args):
        while not self._periodic_msg_disabled.is_set():
            self.logger.info('Sending another periodic message...')
            try:
                response = function(*args)
            except Exception as e:
                self.logger.info('Message function threw an exception: %s', str(e))
                break
            else:
                self.logger.info('RESPONSE MESSAGE: %s', self.getResultString(response))
                if not self.resultWasSuccess(response):
                    break

            self._periodic_msg_disabled.wait(interval)
        self.logger.debug('Periodic job thread stopping')
        # in case we exited with an exception
        self._periodic_msg_disabled.set()

    def stopPeriodicMessage(self):
        self.logger.info('Stopping periodic job...')

        self._periodic_msg_disabled.set()

        self._periodic_msg.join()
        self.logger.info('Periodic job stopped')

    def periodicMessageRunning(self):
        return self._periodic_msg and self._periodic_msg.isAlive()

    def initializeReceiveSocket(self):
        return self.openReceiveSocket()

    def openReceiveSocket(self):

        self.__enforce_receive_host_and_port()

        if self.__to_use_at_sockets():
            self.network.open_receive_socket(self.receive_port)
            return True

        with self._receive_cv:
            self._receive_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.logger.info('Socket created')
        
        self.open_receive_socket_helper()

        return True

    # EFFECTS: Opens and binds an inbound socket connection.
    def open_receive_socket_helper(self):

        with self._receive_cv:
            # Try to bind to the socket if it's already initialized.
            try:
                self.logger.info('Binding to socket...')
                self._receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._receive_socket.bind((self.receive_host, self.receive_port))
            except socket.error:
                self.logger.info('Retrying...')
                try:
                    self._receive_socket.shutdown(socket.SHUT_RDWR)
                except socket.error:
                    pass
                self._receive_socket.close()
                self._receive_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._receive_socket.bind((self.receive_host, self.receive_port))

            # Set socketClose back to False since we're opening it again.
            self.socketClose = False
            self.sock = None
            self._is_send_socket_open = False

            # become a server socket
            self._receive_socket.listen(MAX_QUEUED_CONNECTIONS)

        # Spin a new thread for accepting incoming operations
        self._accept_thread = threading.Thread(target=self.acceptIncomingConnection)
        self._accept_thread.daemon = True
        self._accept_thread.start()

    # EFFECTS: Closes the inbound socket connection.
    def closeReceiveSocket(self):

        self.logger.info('Closing socket...')

        if self.__to_use_at_sockets():
            self.network.close_socket()
            return

        with self._receive_cv:
            self.socketClose = True
        
        self._accept_thread.join()

        with self._receive_cv:
            try:
                self._receive_socket.shutdown(socket.SHUT_RDWR)
            except socket.error:
                pass
            self._receive_socket.close()

        self.logger.info('Socket closed.')

    def acceptIncomingConnection(self):
        try:
            self._receive_socket.setblocking(0)
            # This threaded infinite loop shoud keep listening on an incoming connection
            while True:
                with self._receive_cv:
                    if self.socketClose:
                        break
                    # Use select to avoid a busy loop here
                    rlist, _, xlist = select([self._receive_socket], [], [self._receive_socket], 1)
                    if self._receive_socket in xlist:
                        break
                    if self._receive_socket not in rlist:
                        continue
                    (clientsocket, address) = self._receive_socket.accept()
                    self.logger.info('Connected to %s', address)
                    # Spin a new thread to handle the current incoming operation.
                    threading.Thread(target=self.__incoming_connection_thread,
                                    args=[clientsocket]).start()
        except:
            self.logger.exception("Exception in accept thread!")

    # EFFECTS: This threaded method accepts an inbound connection and appends
    #          the received message onto the receive buffer.
    #          It also broadcasts the message.received event
    def __incoming_connection_thread(self, clientsocket):
        try:
            # Keep parsing the received data until timeout or receive no more data.
            # NOTE: the client doesn't close the socket so this always waits for timeout
            recv = recvall(clientsocket, timeout=RECEIVE_TIMEOUT, encoding='utf-8')
            if not recv:
                self.logger.info("Received empty message!")
                return

            self.logger.info('Received message: %s', recv)

            with self._receive_buffer_lock:
                # Append received message into receive buffer
                self._receive_buffer.append(recv)
                self.logger.debug('Receive buffer: %s', self._receive_buffer)

            self.event.broadcast('message.received')
        except:
            self.logger.exception("Exception in incoming connection thread!")
        finally:
            try:
                clientsocket.close()
            except:
                pass

    # EFFECTS: Returns the receive buffer and empties it.
    def popReceivedMessage(self):

        if self.__to_use_at_sockets():
            return self.network.pop_received_message()

        with self._receive_buffer_lock:
            if len(self._receive_buffer) == 0:
                data = None
            else:
                data = self._receive_buffer.popleft()

        return data

    # EFFECTS: Makes sure that the send host and port are set before making
    #          outbound connection.
    def __enforce_send_host_and_port(self):
        if self.send_host == '' or self.send_port == 0:
            raise HologramError('Send host and port must be set before making this operation')

    # EFFECTS: Makes sure that the receive host and port are set before making
    #          an inbound connection.
    def __enforce_receive_host_and_port(self):
        if self.receive_host == '' or self.receive_port == 0:
            raise HologramError('Receive host and port must be set before making this operation')

    def _enforce_minimum_periodic_interval(self, interval):
        if interval < MIN_PERIODIC_INTERVAL:
            raise HologramError('Interval cannot be less than %d seconds.' % MIN_PERIODIC_INTERVAL)

    def __enforce_network_disconnected(self):
        if self.network_type == 'Cellular':
            self.network.disconnect()

    def getResultString(self, result_code):
        return str(result_code)

    def resultWasSuccess(self, result_code):
        return True
