import argparse
import json
from socket import *
import threading

def start_server(ip: str, port: int):
    SERVER_ADDRESS = (ip, port) # (IP, PORT)
    with socket(AF_INET, SOCK_STREAM) as serverSocket: # -- SOCK_STREAM: TCP Connection, AF_INET: Family Address = IPV4 
        serverSocket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1) # To allow restart when connection is dropped not by intettion
        serverSocket.bind(SERVER_ADDRESS) # bind IP+PORT
        serverSocket.listen(1) # allow one connection
        print("The server is up on {ip}:{port} and is ready to crash epiclly!!!")
        while True: # Make the connection persistent
            conn_socket, addr = serverSocket.accept() # conn_socket: socket ,Client address (ip: str, port: int)
            threading.Thread(target=handle_stream, args=(conn_socket, addr), daemon=True).start()  # By making it async we will be able
                                                                                                    # To make in parrallel in the requests

class DataLinkLayer:
    # Total (14 bytes) each hex 00 = 1 byte
    def __init__(self, hexStr: str):
        self.MacDst = hexStr[:12] # (6 bytes) 
        self.MacSrc =  hexStr[12:24] # (6 bytes)
        self.Type = hexStr[24:28] # (2 bytes)

class NetworkLayer:

    def __init__(self, hexStr: str):
        self.version = hexStr[:2] # (1 byte)
        self.dsf = hexStr[2:4] # (1 byte)
        self.total_length = hexStr[4:8] # (2 bytes)
        self.identification = hexStr[8:12] # (2 bytes)
        self.flags_and_offset = hexStr[12:16] # (2 bytes)
        self.ttl = hexStr[16:18] # (1 bytes)
        self.protocol = hexStr[18:20] # (1 bytes)
        self.checksum = hexStr[20:24] # (2 bytes)
        self.dst_address = hexStr[24:32] # (4 bytes)
        self.src_address = hexStr[32:40] # (4 bytes)

class TransportLayer:
    def __init__(self, hesStr: str):
        


def handle_packets(packet):
    # This is the main LOGIC of the MATALA
    # The message comes in Hex Format
    print("The message bedore decoding is: \n {packet} \n")






    pass

def handle_stream(conn: socket, addr: tuple[str, int]):
    with conn:
        try:
            buff = b"" # We recive everyhing into a Byte string with chunks (cause of Fragmentetion)

            while True:
                chunk = conn.recv(4096) # Read up to 4KB 
                if not chunk:
                    break
                buff += chunk
                if b"\n" in buff: # Check for delimiter
                    raw_json_line, _, rest = buff.partition(b"\n")
                    buff = rest
                    packet = json.loads(raw_json_line.decode("utf-8"))
                    resp_packet = handle_packets(packet)
                    out = (json.dumps(resp_packet) + "\n").encode("utf-8")
                    # return packet from server to client
                    conn.sendall(out)
        except Exception as e:
                try:
                    conn.sendall((json.dumps({"ok": False, "error": f"Malformed: {e}"} ) + "\n").encode("utf-8"))
                except Exception:
                    pass


def main():
    print(f"starting the shitty Server")
    ap = argparse.ArgumentParser(description="JSON TCP server (MATALA3)")
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()
    start_server()


if __name__ == "__main__":
    main()