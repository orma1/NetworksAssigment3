import argparse
import json
import socket
import threading
import random # [Added] Needed for dynamic resizing logic

# --- TCP FLAG CONSTANTS ---
FLAG_FIN = 0x01
FLAG_SYN = 0x02
FLAG_RST = 0x04
FLAG_PSH = 0x08
FLAG_ACK = 0x10

# --- SERVER CONFIG ---
SERVER_CONFIG = {
    "max_msg_size": 20,   # Initial default
    "dynamic_window": True # Enable the dynamic behavior [cite: 212]
}

class ConnectionState:
    """Tracks sequence numbers and buffer for ONE client"""
    def __init__(self):
        self.state = "LISTEN"
        self.expected_seq = 0
        self.buffer = {}
        # Track the current limit for this specific connection
        self.current_max_msg_size = SERVER_CONFIG["max_msg_size"]

def handle_packets(packet, state: ConnectionState):
    """
    The Brain: Processes the packet using Bit-Flags and updates State.
    """
    flags = packet.get("flags", 0) # TODO: Either we want to explain this edge case, or we just replace it with unsafe code
    seq_num = packet.get("seq", 0)
    
    # Debug print
    flag_names = []
    if flags & FLAG_SYN: flag_names.append("SYN")
    if flags & FLAG_ACK: flag_names.append("ACK")
    if flags & FLAG_PSH: flag_names.append("PSH")
    if flags & FLAG_FIN: flag_names.append("FIN")
    # print(f"[{state.state}] Packet: {'|'.join(flag_names)} (Seq={seq_num})")

    # --- PHASE 1: HANDSHAKE (SYN) ---
    if state.state == "LISTEN":
        if flags & FLAG_SYN:
            state.state = "SYN_RCVD"
            # SYN consumes one Sequence Number (Seq 0 -> Expect 1)
            state.expected_seq += 1 
            
            return {
                "flags": FLAG_SYN | FLAG_ACK, 
                "ack": 0, 
                "max_msg_size": state.current_max_msg_size,
                "dynamic_window": SERVER_CONFIG["dynamic_window"]
            }

    # --- PHASE 2: FINISH HANDSHAKE (ACK) ---
    elif state.state == "SYN_RCVD":
        if (flags & FLAG_ACK) and not (flags & FLAG_SYN):
            state.state = "ESTABLISHED"
            print(">>> Connection ESTABLISHED <<<")
            return None 

    # --- PHASE 3: DATA TRANSFER (PSH) ---
    elif state.state == "ESTABLISHED":
        
        # 1. Handle Data (PSH)
        if flags & FLAG_PSH:
            
            # --- DYNAMIC SIZE ENFORCEMENT ---
            # If the client sent a packet bigger than allowed, DROP IT.
            payload = packet.get("payload", "")
            if len(payload) > state.current_max_msg_size:
                print(f"[!] Dropping Oversized Seq {seq_num} ({len(payload)} > {state.current_max_msg_size})")
                # Send ACK for the last valid packet to force Client Timeout & Resize
                return {
                    "flags": FLAG_ACK, 
                    "ack": state.expected_seq - 1,
                    "max_msg_size": state.current_max_msg_size, # <-- Sending in the ACK the new valid size
                    "dynamic_window": SERVER_CONFIG["dynamic_window"] 
                }

            # A. In-Order
            if seq_num == state.expected_seq:
                print(f"   -> Accepted Data Seq {seq_num} | (M{seq_num})")
                state.expected_seq += 1 # We got N, now we expect N+1
                
                # Check Buffer for gaps we can now fill 
                while state.expected_seq in state.buffer: 
                    print(f"   -> Buffer Fill Seq {state.expected_seq}")
                    del state.buffer[state.expected_seq]
                    state.expected_seq += 1
                
                # --- [ADDED] DYNAMIC RESIZING LOGIC ---
                # "The server can also send the flag 'dynamic message size = true'"
                response = {"flags": FLAG_ACK, "ack": state.expected_seq - 1}
                
                if SERVER_CONFIG["dynamic_window"] and random.random() < 0.2: # 20% chance to change
                    # 3:1 Bias: 75% chance to grow/stay, 25% chance to shrink
                    change_factor = random.choices([1.5, 0.5], weights=[0.75, 0.25])[0]
                    new_size = int(state.current_max_msg_size * change_factor)
                    new_size = max(10, min(new_size, 1024)) # Clamp values
                    
                    if new_size != state.current_max_msg_size:
                        print(f"[Dynamic] Resizing Max Msg to {new_size}")
                        state.current_max_msg_size = new_size
                        response["max_msg_size"] = new_size # Client will see this and wipe map
                
                return response

            # B. Out-of-Order (Buffer it) 
            elif seq_num > state.expected_seq:
                print(f"   -> Out-of-Order! Buffering Seq {seq_num}, Waiting for {state.expected_seq}")
                state.buffer[seq_num] = packet
                return {"flags": FLAG_ACK, "ack": state.expected_seq - 1}

            # C. Duplicate/Old
            elif seq_num < state.expected_seq:
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
            buff = b"" 
            while True:
                chunk = conn.recv(4096) 
                if not chunk: break
                buff += chunk
                
                while b"\n" in buff:
                    raw_json_line, _, rest = buff.partition(b"\n")
                    buff = rest
                    
                    try:
                        packet = json.loads(raw_json_line.decode("utf-8"))
                        resp_packet = handle_packets(packet, state) 
                        if resp_packet:
                            out = (json.dumps(resp_packet) + "\n").encode("utf-8")
                            conn.sendall(out)
                    except json.JSONDecodeError:
                        print("Error: Malformed JSON")

        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": f"Malformed: {e}"} ) + "\n").encode("utf-8"))
            except Exception:
                pass

def start_server(ip: str, port: int):
    SERVER_ADDRESS = (ip, port) 
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as serverSocket:
        serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        serverSocket.bind(SERVER_ADDRESS)
        serverSocket.listen(5)
        print(f"Server is up on {ip}:{port} and is ready to crash!!!")
        while True:
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