import argparse
import json
import socket
import threading

# --- TCP FLAG CONSTANTS ---
FLAG_FIN = 0x01
FLAG_SYN = 0x02
FLAG_RST = 0x04
FLAG_PSH = 0x08
FLAG_ACK = 0x10

# --- SERVER CONFIG ---
# (In the final version, load these from config.txt)
SERVER_CONFIG = {
    "max_msg_size": 20,
    "dynamic_window": False
}

class ConnectionState:
    """Tracks sequence numbers and buffer for ONE client"""
    def __init__(self):
        self.state = "LISTEN"  # LISTEN -> SYN_RCVD -> ESTABLISHED -> FIN_WAIT
        self.expected_seq = 0
        self.buffer = {}

def handle_packets(packet, state: ConnectionState):
    """
    The Brain: Processes the packet using Bit-Flags and updates State.
    """
    flags = packet.get("flags", 0)
    seq_num = packet.get("seq", 0)
    
    # Debug print to see what's happening
    flag_names = []
    if flags & FLAG_SYN: flag_names.append("SYN")
    if flags & FLAG_ACK: flag_names.append("ACK")
    if flags & FLAG_PSH: flag_names.append("PSH")
    if flags & FLAG_FIN: flag_names.append("FIN")
    print(f"[{state.state}] Packet: {'|'.join(flag_names)} (Seq={seq_num})")

    # --- PHASE 1: HANDSHAKE (SYN) ---
    if state.state == "LISTEN":
        if flags & FLAG_SYN:
            state.state = "SYN_RCVD"
            state.expected_seq += 1
            # Return SYN-ACK (0x12)
            return {
                "flags": FLAG_SYN | FLAG_ACK, 
                "ack": 0, 
                "max_msg_size": SERVER_CONFIG["max_msg_size"]
            }

    # --- PHASE 2: FINISH HANDSHAKE (ACK) ---
    elif state.state == "SYN_RCVD":
        if (flags & FLAG_ACK) and not (flags & FLAG_SYN):
            state.state = "ESTABLISHED"
            print(">>> Connection ESTABLISHED <<<")
            return None  # No reply needed

    # --- PHASE 3: DATA TRANSFER (PSH) ---
    elif state.state == "ESTABLISHED":
        
        # 1. Handle Data (PSH)
        if flags & FLAG_PSH:
            # A. In-Order
            if seq_num == state.expected_seq:
                print(f"   -> Accepted Data Seq {seq_num}")
                state.expected_seq += 1
                
                # Check Buffer for gaps we can now fill
                while state.expected_seq in state.buffer:
                    print(f"   -> Buffer Fill Seq {state.expected_seq}")
                    del state.buffer[state.expected_seq]
                    state.expected_seq += 1
                
                # Send Cumulative ACK
                return {"flags": FLAG_ACK, "ack": state.expected_seq - 1}

            # B. Out-of-Order (Buffer it)
            elif seq_num > state.expected_seq:
                print(f"   -> Out-of-Order! Buffering Seq {seq_num}, Waiting for {state.expected_seq}")
                state.buffer[seq_num] = packet
                # Re-ACK the last good one
                return {"flags": FLAG_ACK, "ack": state.expected_seq - 1}

            # C. Duplicate/Old
            elif seq_num < state.expected_seq:
                print(f"   -> Duplicate Seq {seq_num}, Ignoring.")
                return {"flags": FLAG_ACK, "ack": state.expected_seq - 1}

        # 2. Handle Connection Close (FIN)
        if flags & FLAG_FIN:
            print("Received FIN. Sending FIN-ACK.")
            return {"flags": FLAG_ACK | FLAG_FIN, "ack": seq_num + 1}

    return None

def handle_stream(conn: socket.socket, addr: tuple[str, int]):
    state = ConnectionState()

    with conn:
        try:
            buff = b"" # We receive everything into a Byte string with chunks
            while True:
                chunk = conn.recv(4096) # Read up to 4KB 
                if not chunk:
                    break
                buff += chunk
                
                if b"\n" in buff: # Check for delimiter
                    raw_json_line, _, rest = buff.partition(b"\n")
                    buff = rest
                    
                    try:
                        packet = json.loads(raw_json_line.decode("utf-8"))
                        
                        # Pass state to the logic function
                        resp_packet = handle_packets(packet, state) 
                        
                        if resp_packet:
                            out = (json.dumps(resp_packet) + "\n").encode("utf-8")
                            conn.sendall(out)
                            
                    except json.JSONDecodeError:
                        print("Error: Malformed JSON received")

        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": f"Malformed: {e}"} ) + "\n").encode("utf-8"))
            except Exception:
                pass

def start_server(ip: str, port: int):
    SERVER_ADDRESS = (ip, port) 
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as serverSocket: # -- SOCK_STREAM: TCP Connection, AF_INET: Family Address = IPV4 
        serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # To allow restart when connection is dropped not by intettion
        serverSocket.bind(SERVER_ADDRESS) # bind IP+PORT
        serverSocket.listen(5)
        print(f"Server is up on {ip}:{port} and is ready to crash!!!")
        while True:  # Make the connection persistent
            conn_socket, addr = serverSocket.accept()
            print(f"New connection from {addr}")
            threading.Thread(target=handle_stream, args=(conn_socket, addr), daemon=True).start()

def main():
    ap = argparse.ArgumentParser(description="JSON TCP server (MATALA3)")
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()
    start_server(args.ip, args.port)

if __name__ == "__main__":
    main()