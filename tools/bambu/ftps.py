"""Implicit FTPS (port 990) to the P2S USB stick; works in cloud or LAN mode."""
import ftplib, socket, ssl

import p2s

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE


class ImplicitFTPS(ftplib.FTP_TLS):
    def connect(self, host, port, timeout=30):
        self.host, self.port = host, port
        self.sock = ctx.wrap_socket(socket.create_connection((host, port), timeout), server_hostname=host)
        self.af = self.sock.family; self.file = self.sock.makefile("r", encoding="latin-1")
        self.welcome = self.getresp(); return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def connect(timeout=30):
    f = ImplicitFTPS(context=ctx); f.connect(p2s.IP, p2s.FTPS_PORT, timeout)
    f.login("bblp", p2s.code()); f.prot_p()
    return f


def download(name, dest, timeout=60):
    """Copy /<name> from the stick to dest."""
    f = connect(timeout)
    try:
        with open(dest, "wb") as fh:
            f.retrbinary("RETR /" + name, fh.write)
    finally:
        try:
            f.quit()
        except Exception:
            f.close()
