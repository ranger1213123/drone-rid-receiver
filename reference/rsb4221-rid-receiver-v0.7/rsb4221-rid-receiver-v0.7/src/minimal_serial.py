# Minimal serial module for RSB-4221 (pure Python)
# Provides serial.Serial needed by rid_serial_receiver

import os, sys, time, struct, termios, fcntl, array, errno

class SerialException(Exception):
    pass

class Serial(object):
    def __init__(self, port=None, baudrate=115200, timeout=1, **kwargs):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._fd = None
        self._is_open = False
        if port:
            self.open()

    def open(self):
        if not os.path.exists(self._port):
            raise SerialException("Port %s not found" % self._port)
        try:
            self._fd = os.open(self._port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise SerialException("Cannot open %s: %s" % (self._port, e))
        self._set_baud(self._baudrate)
        self._set_attributes()
        self._is_open = True

    def _set_baud(self, baud):
        baud_map = {
            9600: termios.B9600, 19200: termios.B19200,
            38400: termios.B38400, 57600: termios.B57600,
            115200: termios.B115200, 230400: termios.B230400,
            460800: termios.B460800, 921600: termios.B921600,
        }
        if baud not in baud_map:
            raise SerialException("Unsupported baud %d" % baud)
        if self._fd is None:
            return
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(self._fd)
            cflag &= ~(termios.PARENB | termios.PARODD | termios.CSTOPB | termios.CRTSCTS | termios.CSIZE)
            cflag |= termios.CLOCAL | termios.CREAD | termios.CS8
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 1
            termios.tcsetattr(self._fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, baud_map[baud], baud_map[baud], cc])
        except:
            pass

    def _set_attributes(self):
        if self._fd is None:
            return
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(self._fd)
            iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
            oflag &= ~termios.OPOST
            lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            termios.tcsetattr(self._fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
        except:
            pass

    def close(self):
        if self._fd is not None and self._is_open:
            try:
                os.close(self._fd)
            except:
                pass
            self._fd = None
            self._is_open = False

    def readline(self):
        """Read a line from serial port"""
        if not self._is_open:
            return b""
        result = b""
        deadline = time.time() + self._timeout if self._timeout else 0
        while True:
            try:
                data = os.read(self._fd, 1)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    if self._timeout and time.time() > deadline:
                        break
                    time.sleep(0.001)
                    continue
                break
            if not data:
                if self._timeout and time.time() > deadline:
                    break
                continue
            result += data
            if data in (b"\n", b"\r"):
                break
        return result

    def read(self, size=1):
        if not self._is_open:
            return b""
        result = b""
        deadline = time.time() + self._timeout if self._timeout else 0
        while len(result) < size:
            try:
                data = os.read(self._fd, size - len(result))
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    if self._timeout and time.time() > deadline:
                        break
                    time.sleep(0.001)
                    continue
                break
            if not data:
                break
            result += data
        return result

    def write(self, data):
        if self._is_open:
            os.write(self._fd, data)

    def flush(self):
        if self._fd is not None:
            try:
                termios.tcflush(self._fd, termios.TCIOFLUSH)
            except:
                pass

    def __del__(self):
        self.close()

    @property
    def is_open(self):
        return self._is_open
