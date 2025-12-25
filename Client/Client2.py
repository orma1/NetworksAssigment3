import argparse
import json
import socket
import threading
import time

# --- NAMING CONVENTIONS & FLAGS ---
# These flags mimic the standard TCP header bits.
# FLAG_FIN (0x01): "Finish". Used to gracefully close the connection.
# FLAG_SYN (0x02): "Synchronize". Used to initiate the 3-way handshake.
# FLAG_PSH (0x08): "Push". Indicates that data is being sent (Payload).
# FLAG_ACK (0x10): "Acknowledgment". Confirm receipt of packets.
FLAG_FIN = 0x01
FLAG_SYN = 0x02
FLAG_PSH = 0x08
FLAG_ACK = 0x10

CLIENT_CONFIG = {
    "server_ip": "127.0.0.1",
    "server_port": 13000,
    "timeout": 5.0,        # Seconds before retransmission
    "window_size": 4       # Max unacknowledged packets allowed in flight 
}

class ClientState:
    """Tracks the Client's View of the Connection"""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "CLOSED"    # CLOSED -> SYNSENT -> ESTABLISHED -> FIN_WAIT
        self.seq_num = 0         # Client's current sequence number (for Handshake)
        self.window_base = 0     # The oldest unacknowledged packet sequence number
        self.max_msg_size = 1024 # Default size, updated dynamically by Server 
        self.window_size = 4     # Sliding window capacity
        
        # Timer Logic
        self.timer_start = None
        self.timeout_value = CLIENT_CONFIG["timeout"]

def handle_packets(packet, state):
    """
    Client Logic: Reacts to Server Packets and updates State (Window, MaxSize).
    """
    flags = packet.get("flags", 0)
    server_ack = packet.get("ack", 0)
    
    # Debug
    # print(f"[RECV] Flags={flags}, Ack={server_ack}")

    # --- 1. HANDLE HANDSHAKE (Server sent SYN-ACK) ---
    # TODO: Make sure if we don't get from the Server back and SYN-ACK, we send after timeout a SYN again.
    if state.state == "SYNSENT":
        if (flags & FLAG_SYN) and (flags & FLAG_ACK):
            print("   >>> Handshake Step 2: Received SYN-ACK")
            
            # Update Params if server sent them (Initial Negotiation) 
            if "max_msg_size" in packet:
                state.max_msg_size = int(packet["max_msg_size"])
                print(f"   >>> Negotiated Max Msg Size: {state.max_msg_size}")

            # Prepare Step 3: Send ACK to complete connection 
            with state.lock:
                state.state = "ESTABLISHED"
                state.seq_num += 1
                # Handshake consumes Seq 0. Window starts at Seq 1.
                state.window_base = 1 
            
            # Respond with ACK
            return {"flags": FLAG_ACK, "seq": state.seq_num, "ack": 0}

    # --- 2. HANDLE DATA ACKS (Server sent ACK) ---
    elif state.state == "ESTABLISHED":
        if flags & FLAG_ACK:
            
            # CHECK FOR DYNAMIC SIZE UPDATE 
            # "If dynamic, it will be sent as part of the ack packet" 
            if "max_msg_size" in packet:
                new_size = int(packet["max_msg_size"])
                with state.lock:
                    if new_size != state.max_msg_size:
                        print(f"[Dynamic] Max Msg Size updated: {state.max_msg_size} -> {new_size}")
                        state.max_msg_size = new_size

            # CUMULATIVE ACK LOGIC
            # "The server needs to acknowledge every message" 
            # We interpret ACK N as "I received everything UP TO N", so expect N+1 next.
            new_base = server_ack + 1
            
            with state.lock:
                if new_base > state.window_base:
                    print(f"[Recv] Got ACK {server_ack}. Sliding Window to {new_base}")
                    state.window_base = new_base
                    
                    # Update Timer:
                    # [cite_start]If the window moves, we restart the timer for the NEW oldest packet. [cite: 10]
                    # If window becomes empty (all sent packets acked), this timestamp 
                    # will effectively be ignored until the Sender adds a new packet.
                    state.timer_start = time.time()

    return None

def three_way_handshake(state):
    """
    Waits for the Receiver Thread (handle_packets) to confirm 
    the connection is ESTABLISHED before we start sending data.
    """
    print("[Sender] Waiting for Handshake...")
    while True:
        with state.lock:
            if state.state == "ESTABLISHED":
                break
        time.sleep(0.1)

def sliding_window(conn: socket.socket, state, buff_data: str, total_len: int, next_seq: int, seq_map: dict):
    """
    The Core Loop: Handles Sending, Window Management, and Retransmission.
    We implemented an HYBRID of GBN and Selective Repreat algorithm
    """
    # --- 3. SLIDING WINDOW LOOP ---
    while True:
        with state.lock: 
            current_base = state.window_base 
            current_max_size = state.max_msg_size  
            window_limit = current_base + state.window_size 
                                              
            # --- COMPLETION CHECK ---
            # Case A: Base is mapped and points to End of File
            if current_base in seq_map and seq_map[current_base] >= total_len:
                break
            
            # Case B: Base moved past our map (Final ACK received)
            if current_base not in seq_map and next_seq > 1:
                 prev_seq = current_base - 1
                 if prev_seq in seq_map and seq_map[prev_seq] >= total_len:
                     break

        # --- A. SEND NEW PACKETS (Fill the Window) ---
        while next_seq < window_limit:
            
            # If we don't have a mapping for this seq yet, we can't send it.
            if next_seq not in seq_map:
                break

            offset = seq_map[next_seq]
            
            # Stop if we reached end of data
            if offset >= total_len:
                break

            # DYNAMIC SLICING: Cut the chunk NOW using current_max_size
            payload = buff_data[offset : offset + current_max_size]
            
            # Calculate where the NEXT packet will start
            seq_map[next_seq + 1] = offset + len(payload)

            packet = {
                "flags": FLAG_PSH, 
                "seq": next_seq,
                "ack": 0, 
                "payload": payload
            }
            
            try:
                print(f"[Sender] Sending Seq {next_seq} (Size: {len(payload)} bytes)...")
                conn.sendall((json.dumps(packet) + "\n").encode("utf-8"))
                
                with state.lock:
                    # Start Timer if this packet is the 'oldest unacknowledged' [cite: 10]
                    if state.window_base == next_seq:
                        state.timer_start = time.time()
                
                next_seq += 1
                
            except Exception as e:
                print(f"[Sender] Error sending: {e}")
                break

        # --- B. CHECK TIMEOUT (Go-Back-N / Dynamic Resize) ---
        with state.lock:
            if state.timer_start is not None:
                elapsed = time.time() - state.timer_start
                if elapsed > state.timeout_value:
                    print(f"[!!!] TIMEOUT ({elapsed:.2f}s)! Resending Window starting at {state.window_base}")
                    
                    # RETRANSMIT LOGIC[cite: 11]:
                    # 1. Reset 'next_seq' to 'window_base' (Go-Back-N)
                    next_seq = state.window_base
                    
                    # 2. MAP WIPE (The Fix for Dynamic Sizing):
                    # We keep the offset for the Base, but wipe future predictions.
                    # This forces the logic above to Recalculate 'seq_map[next+1]' 
                    # using the NEW 'current_max_size'.
                    if next_seq in seq_map:
                        base_offset = seq_map[next_seq]
                        seq_map = {next_seq: base_offset} 
                    
                    # 3. Restart timer [cite: 12]
                    state.timer_start = time.time() 
        
        time.sleep(0.01) # Prevent CPU burn
    
    # Finished sending all data, proceed to teardown
    fin_four_step_handshake(conn, next_seq)

def fin_four_step_handshake(conn: socket.socket, next_seq: int):
    # --- 4. TEARDOWN (FIN) ---
    print("[Sender] All data acknowledged. Sending FIN...")
    # [cite: 166] The client initiates the close
    fin_packet = {"flags": FLAG_FIN, "seq": next_seq, "ack": 0}
    conn.sendall((json.dumps(fin_packet) + "\n").encode("utf-8"))
    
    # TODO: strictly implement "4-Way Handshake":
    # 1. We send FIN (Done above)
    # 2. We wait for ACK from Server (Handled in handle_packets)
    # 3. We wait for FIN from Server (Need to add logic in handle_packets to detect this)
    # 4. We send final ACK (Need to add logic in handle_packets)

def sender_logic(conn: socket.socket, state, data_source: str):
    """
    Main Sender Loop: Orchestrates the connection lifecycle.
    """
    # 1. Handshake
    three_way_handshake(state)

    print("[Sender] Connection Established. Starting Dynamic Data Transfer...")
    
    # 2. Prepare Data
    buff_data = data_source 
    total_len = len(buff_data)
    
    # Map Sequence Numbers to Byte Offsets
    # seq 1 maps to index 0 of the string
    seq_map = {1: 0} 
    next_seq = 1 
    
    # 3. Transfer Data
    sliding_window(conn, state, buff_data, total_len, next_seq, seq_map)


def handle_stream(conn: socket.socket, addr: tuple[str, int], state: ClientState):
    """
    Main Receiver Loop. 
    Listens for packets and updates SHARED 'state'.
    Does NOT initiate Handshake (start_client does that).
    """
    with conn:
        try:
            # --- RECEIVE LOOP ---
            buff = b"" 
            while True:
                chunk = conn.recv(4096) 
                if not chunk:
                    break
                buff += chunk
                
                while b"\n" in buff:
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

        # 4. Initiate Handshake (Client Speaks First) [cite: 19]
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