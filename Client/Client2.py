import argparse
import json
import socket
import threading
import time

# --- FLAGS & CONFIG (Must match Server) ---
FLAG_FIN = 0x01
FLAG_SYN = 0x02
FLAG_PSH = 0x08
FLAG_ACK = 0x10

CLIENT_CONFIG = {
    "server_ip": "127.0.0.1",
    "server_port": 13000,
    "timeout": 5
}

class ClientState:
    """Tracks the Client's View of the Connection"""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "CLOSED"  # CLOSED -> SYNSENT -> ESTABLISHED -> FIN_WAIT
        self.seq_num = 0       # Current Sequence Number
        self.window_base = 0
        self.max_msg_size = 1024 # Will be updated by Server
        self.window_size = 4   # Window size (packets)
        # Timer Logic
        self.timer_start = None
        self.timeout_value = 5.0 # Seconds


def sender_logic(conn: socket.socket, state: ClientState, data_source: str):
    """
    Main Sender Loop:
    1. Segments data.
    2. Sends packets respecting the Window.
    3. Handles Retransmission on Timeout.
    """
    
    # --- 1. WAIT FOR HANDSHAKE ---
    print("[Sender] Waiting for Handshake...")
    while True:
        with state.lock:
            if state.state == "ESTABLISHED":
                break
        time.sleep(0.1)

    print("[Sender] Connection Established. preparing data...")
    
    # --- 2. SEGMENTATION ---
    # In reality, read this from the file path provided in 'data_source'
    # For simulation, we assume 'data_source' IS the string data.
    full_data = data_source 
    chunks = [full_data[i : i + state.max_msg_size] for i in range(0, len(full_data), state.max_msg_size)]
    total_chunks = len(chunks)
    
    print(f"[Sender] Data split into {total_chunks} chunks (Max Size: {state.max_msg_size})")

    # --- 3. SLIDING WINDOW LOOP ---
    base_seq = 1 # We start at 1 because Handshake used 0
    next_seq = 1 # Pointer to the next packet to send
    
    # Map Sequence Numbers to Data Chunks for Retransmission
    # packet_buffer = {seq_num: payload}
    packet_buffer = {} 
    for i, chunk in enumerate(chunks):
        packet_buffer[base_seq + i] = chunk

    last_seq = base_seq + total_chunks - 1

    while state.window_base <= last_seq:
        
        with state.lock:
            # Sync our local view with the thread-safe state
            current_window_base = state.window_base
            window_limit = current_window_base + state.window_size

        # --- A. SEND NEW PACKETS (If Window is Open) ---
        while next_seq < window_limit and next_seq <= last_seq:
            payload = packet_buffer[next_seq]
            
            # Construct Data Packet (PSH | ACK)
            packet = {
                "flags": FLAG_PSH | FLAG_ACK,
                "seq": next_seq,
                "ack": 0, # Client ACKs are usually 0 unless we are bidirectional
                "payload": payload
            }
            
            try:
                print(f"[Sender] Sending Seq {next_seq}...")
                conn.sendall((json.dumps(packet) + "\n").encode("utf-8"))
                
                # Start Timer if it's the first in the window
                with state.lock:
                    if state.window_base == next_seq:
                        state.timer_start = time.time()
                
                next_seq += 1
                
            except Exception as e:
                print(f"[Sender] Error sending: {e}")
                break

        # --- B. CHECK TIMEOUT (Go-Back-N) ---
        with state.lock:
            if state.timer_start is not None:
                elapsed = time.time() - state.timer_start
                if elapsed > state.timeout_value:
                    print(f"[!!!] TIMEOUT ({elapsed:.2f}s)! Resending Window starting at {state.window_base}")
                    
                    # RETRANSMIT LOGIC:
                    # Reset 'next_seq' to 'window_base' so the loop above resends them
                    next_seq = state.window_base
                    state.timer_start = time.time() # Restart timer
        
        time.sleep(0.01) # Prevent CPU burn

    # --- 4. TEARDOWN (FIN) ---
    print("[Sender] All data acknowledged. Sending FIN...")
    fin_packet = {"flags": FLAG_FIN | FLAG_ACK, "seq": next_seq, "ack": 0}
    conn.sendall((json.dumps(fin_packet) + "\n").encode("utf-8"))


def handle_packets(packet, state: ClientState):
    """
    Client Logic: Reacts to Server Packets and returns the NEXT packet to send.
    """
    flags = packet.get("flags", 0)
    server_ack = packet.get("ack", 0)
    
    # Debug
    print(f"[RECV] Flags={flags}, Ack={server_ack}")

    # --- 1. HANDLE HANDSHAKE (Server sent SYN-ACK) ---
    if state.state == "SYNSENT":
        if (flags & FLAG_SYN) and (flags & FLAG_ACK):
            print("   >>> Handshake Step 2: Received SYN-ACK")
            
            # Update Params if server sent them [cite: 4]
            if "max_msg_size" in packet:
                state.max_msg_size = packet["max_msg_size"]
                print(f"   >>> Negotiated Max Msg Size: {state.max_msg_size}")

            # Prepare Step 3: Send ACK
            state.state = "ESTABLISHED"
            state.seq_num += 1
            
            # Respond with ACK
            return {"flags": FLAG_ACK, "seq": state.seq_num, "ack": 0}

    # --- 2. HANDLE DATA ACKS (Server sent ACK) ---
    elif state.state == "ESTABLISHED":
        if flags & FLAG_ACK:
            # Server sends Cumulative ACK (N). 
            # It means "I have received everything UP TO N".
            # So the NEXT packet expected is N + 1.
            
            # Note: Your assignment logic might imply ACK N means "I got N".
            # Adjust based on exact spec. Standard TCP: ACK N = Expecting N.
            # Let's assume: ACK N = "I received packet N".
            
            new_base = server_ack + 1
            
            with state.lock:
                if new_base > state.window_base:
                    print(f"[Recv] Got ACK {server_ack}. Sliding Window to {new_base}")
                    state.window_base = new_base
                    # Reset Timer logic is handled by Sender checking window_base
                    state.timer_start = time.time() # Restart timer for the new oldest packet

    return None
def handle_stream(conn: socket.socket, addr: tuple[str, int], state: ClientState):
    """
    Main Receiver Loop. 
    Now accepts the SHARED 'state' object so updates are seen by the Sender.
    """
    # REMOVED: state = ClientState()  <-- This was the bug!
    
    with conn:
        try:
            # --- TRIGGER: Client Must Speak First (Send SYN) ---
            print(">>> Initiating Handshake (Sending SYN)...")
            
            # Update the SHARED state (not a local one)
            state.state = "SYNSENT"
            syn_packet = {"flags": FLAG_SYN, "seq": state.seq_num, "ack": 0}
            conn.sendall((json.dumps(syn_packet) + "\n").encode("utf-8"))

            # --- RECEIVE LOOP ---
            buff = b"" 
            while True:
                chunk = conn.recv(4096) 
                if not chunk:
                    break
                buff += chunk
                
                if b"\n" in buff:
                    raw_json_line, _, rest = buff.partition(b"\n")
                    buff = rest
                    
                    try:
                        packet = json.loads(raw_json_line.decode("utf-8"))
                        
                        # Process packet and update the SHARED state
                        resp_packet = handle_packets(packet, state)
                        
                        if resp_packet:
                            out = (json.dumps(resp_packet) + "\n").encode("utf-8")
                            conn.sendall(out)
                            
                    except json.JSONDecodeError:
                        print("Error: Malformed JSON received")

        except Exception as e:
            print(f"Connection Error: {e}")


def start_client(ip: str, port: int):
    SERVER_ADDRESS = (ip, port)
    
    # 1. Create Shared State
    state = ClientState()
    
    # 2. Connect Socket
    # We create the socket OUTSIDE the 'with' block of handle_stream 
    # so we can pass it to the sender thread too.
    clientSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    clientSocket.settimeout(None) # Blocking mode for recv
    
    try:
        clientSocket.connect(SERVER_ADDRESS)
        print(f"Connected to {ip}:{port}")
        
        # 3. Start Sender Thread (Background)
        # This simulates reading from a file and sending chunks
        dummy_file_data = "This is a long message that demonstrates the Go-Back-N sliding window protocol logic." * 5
        
        sender_thread = threading.Thread(
            target=sender_logic,
            args=(clientSocket, state, dummy_file_data),
            daemon=True
        )
        sender_thread.start()

        # 4. Initiate Handshake (Client Speaks First)
        print(">>> Initiating Handshake (Sending SYN)...")
        state.state = "SYNSENT"
        syn_packet = {"flags": FLAG_SYN, "seq": 0, "ack": 0}
        clientSocket.sendall((json.dumps(syn_packet) + "\n").encode("utf-8"))
        
        # 5. Enter Receiver Loop (Main Thread)
        # This will block until connection closes
        handle_stream(clientSocket, SERVER_ADDRESS, state)

    except ConnectionRefusedError:
        print("Error: Could not connect. Is the server running?")
    except Exception as e:
        print(f"Client Error: {e}")
    finally:
        clientSocket.close()

def main():
    ap = argparse.ArgumentParser(description="JSON TCP Client (MATALA3)")
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()
    start_client(args.ip, args.port)

if __name__ == "__main__":
    main()