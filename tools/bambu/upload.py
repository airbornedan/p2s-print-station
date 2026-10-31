"""Upload one file to the P2S USB stick over implicit FTPS (port 990); works in cloud or LAN mode.
Needs a USB drive in the screen-box port, or the printer reports sdcard:false and there is nowhere to store it.
    python3 upload.py <file>"""
import ftplib, os, socket, ssl, sys, p2s
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
path = sys.argv[1]; name = os.path.basename(path)
f = ImplicitFTPS(context=ctx); f.connect(p2s.IP, p2s.FTPS_PORT); f.login("bblp", p2s.code()); f.prot_p()
with open(path, "rb") as fh:
    f.storbinary("STOR /" + name, fh)
remote, local = f.size("/" + name), os.path.getsize(path)
f.quit()
print("uploaded %s: %d bytes on the stick (local %d)" % (name, remote, local))
if remote != local:
    sys.exit("upload incomplete: %d of %d bytes" % (remote, local))
