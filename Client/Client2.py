import argparse
import json
import pathlib
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
        self.state = "CLOSED"    # CLOSED -> THREE_WAY_HANDSHAKE -> REQ_SIZE -> DATA_TRANSFER -> WAIT_FOR_FIN_ACK -> FIN_ACK
        self.seq_num = 0         # Client's current sequence number (for Handshake)
        self.window_base = 0     # The oldest unacknowledged packet sequence number
        self.max_msg_size = 1024 # Default size, updated dynamically by Server 
        self.window_size = 4     # Sliding window capacity
        self.dynamic_message_size = False
        # Timer Logic
        self.timer_start = None
        self.timeout_value = CLIENT_CONFIG["timeout"]
        self.file = False

def handle_packets(packet, state):
    """
    Client Logic: Reacts to Server Packets and updates State (Window, MaxSize).
    """
    flags = packet.get("flags", 0)
    server_ack = packet.get("ack", 0)
    server_seq = packet.get("seq", 0)
    # Debug
    # print(f"[RECV] Flags={flags}, Ack={server_ack}")

    # --- 1. HANDLE HANDSHAKE (Server sent SYN-ACK) ---

    if state.state == "THREE_WAY_HANDSHAKE":
        if (flags & FLAG_SYN) and (flags & FLAG_ACK):
            print("   >>> Handshake Step 2: Received SYN-ACK")
            #if we have max message size in packet, we update it
            if "max_msg_size" in packet:
                state.max_msg_size = int(packet["max_msg_size"])

            # Use the explicit flag if provided, otherwise default to False
            if "dynamic_window" in packet:
                state.dynamic_message_size = packet["dynamic_window"]
                print(f"   >>> Server Dynamic Mode: {state.dynamic_message_size}")
            else:
                # Fallback: If server didn't send the flag but sent a size,
                # you might want to default to True or False depending on preference.
                state.dynamic_message_size = False
                # --- FIX END ---


            # Prepare Step 3: Send ACK to complete connection 
            with state.lock:
                state.state = "REQ_SIZE"
                #state.seq_num += 1
                # Handshake consumes Seq 0. Window starts at Seq 1.
                state.window_base = 1 
            
            # Respond with ACK
                #state.seq_num += 1 #Ack consumes 1 seq_num
            return {"flags": FLAG_ACK, "ack": 0, "dynamic_message_size": state.dynamic_message_size}
        elif state.timer_start is not None:
                elapsed = time.time() - state.timer_start
                if elapsed > state.timeout_value:
                    print(f"[!!!] TIMEOUT ({elapsed:.2f}s)! Resending Syn packet")
    #if connection is established we need to ask for initial message size
    #this happens no matter if message size is dynamic or not
    elif state.state == "REQ_SIZE":
        #we wait for an answer from the server
        if (flags & FLAG_ACK) and (flags & FLAG_PSH):
            # if the size is included, we print a message
            if "max_msg_size" in packet:
                new_size = int(packet["max_msg_size"])
                print(f"   >>> [Negotiation] Server confirmed size: {new_size}")
                #after the message we can set the size and move the state to data_transfer
                with state.lock:
                    state.max_msg_size = new_size
                    state.state = "DATA_TRANSFER"  # Unlock the sender thread
                    # Update window base because we consumed a sequence number for the request
                    state.window_base = server_ack

    elif state.state == "DATA_TRANSFER":
        if flags & FLAG_ACK:
            
            # CHECK FOR DYNAMIC SIZE UPDATE 
            # "If dynamic, it will be sent as part of the ack packet" 
            if state.dynamic_message_size and "max_msg_size" in packet:
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
    # Case B: We receive the Server's FIN -> Send Final ACK -> Close
    # Note: We might receive FIN in FIN_WAIT_2 (normal) or FIN_WAIT_1 (simultaneous close)
    elif state.state == "Wait_for_FIN_ACK":
        if (flags & FLAG_ACK) and not (flags & FLAG_FIN):
            print("   >>> [Recv] Step 2: Server ACKed our FIN. Now waiting for Server FIN...")
            with state.lock:
                # We move to the next state, but we DON'T return yet.
                # The same packet might contain the FIN (Piggybacking).
                state.state = "FIN_ACK"
    #if state.state == "FIN_ACK":
    if state.state in ["Wait_for_FIN_ACK", "FIN_ACK"]:
        if flags & FLAG_FIN:
            print("   >>> [Recv] Step 3: Server sent FIN.")

            # Step 4: Send Final ACK
            ack_to_send = server_seq + 1

            with state.lock:
                state.state = "CLOSED"

            print(f"   >>> [Send] Step 4: Sending Final ACK ({ack_to_send}). Connection CLOSED.")
            return {"flags": FLAG_ACK, "seq": state.seq_num, "dynamic_message_size": state.dynamic_message_size}

    return None

def three_way_handshake(state):
    """
    Waits for the Receiver Thread (handle_packets) to confirm 
    the connection is ESTABLISHED before we start sending data.
    """
    print("[Sender] Waiting for Handshake...")
    while True:
        with state.lock:
            if state.state == "REQ_SIZE":
                break
        time.sleep(0.1)

def sliding_window(conn: socket.socket, state, buff_data: str, total_len: int, next_seq: int, seq_map: dict):
    """
    The Core Loop: Handles Sending, Window Management, and Retransmission.
    We implemented an HYBRID of GBN and Selective Repeat algorithm
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
    fin_four_step_handshake(conn, next_seq, state)

def fin_four_step_handshake(conn: socket.socket, next_seq: int, state: ClientState):
    # --- 4. TEARDOWN (FIN) ---
    print("[Sender] All data acknowledged. Sending FIN...")
    with state.lock:
        state.state = "Wait_for_FIN_ACK"
        # Seq matches the next expected sequence number
        # we create a new packet with the fin flag to send to the server
        fin_packet = {"flags": FLAG_FIN, "seq": next_seq}

    print("Step 1: Sending FIN. State -> Wait_for_FIN_ACK")
    #send the packet to the server
    conn.sendall((json.dumps(fin_packet) + "\n").encode("utf-8"))

    # --- STEP 2: Wait for ACK from the server ---
    print("Waiting for Server to ACK our FIN...")
    while True:
        with state.lock:
            #if we got the fin_ack we can finish waiting
            if state.state == "FIN_ACK" or state.state == "CLOSED":
                break
        time.sleep(0.1)

    # --- STEP 3: Wait for Server's FIN (handled by receiver) ---
    print("[Teardown] Server acknowledged. Now waiting for Server to close the connection...")
    while True:
        with state.lock:
            # The receiver deals with the incoming FIN and transitions to CLOSED
            if state.state == "CLOSED":
                break
        time.sleep(0.1)

    print("[Teardown] Connection Closed Cleanly.")

    # 1. We send FIN (Done above)
    # 2. We wait for ACK from Server (Handled in handle_packets)
    # 3. We wait for FIN from Server (Need to add logic in handle_packets to detect this)
    # 4. We send final ACK (Need to add logic in handle_packets)


def ask_size(conn, state):
    """
    Sends a request to the server to get the max message size parameter.
    Waits until the Receiver thread updates state to DATA_TRANSFER.
    """
    print("[Sender] Handshake done. Requesting Message Size...")
    req_payload = "REQ_SIZE"
    with state.lock:
        state.state = "REQ_SIZE"
        state.seq_num += 1

    packet = {
        "flags": FLAG_PSH,
        "seq": state.seq_num,
        "payload": req_payload
    }

    conn.sendall((json.dumps(packet) + "\n").encode("utf-8"))

    # Wait for response (Receiver thread will switch state to DATA_TRANSFER)
    while True:
        with state.lock:
            if state.state == "DATA_TRANSFER":
                # We need to increment our sequence number counter
                state.seq_num += 1
                break
        time.sleep(0.1)

    print(f"[Sender] Size Updated: {state.max_msg_size}. Starting Data Transfer.")

def sender_logic(conn: socket.socket, state: ClientState, data_source: str):
    """
    Main Sender Loop: Orchestrates the connection lifecycle.
    """
    # 1. Handshake
    three_way_handshake(state)
    #we ask after handshake for initial message size
    ask_size(conn, state)
    print("[Sender] Connection Established. Starting Dynamic Data Transfer...")
    #after the handshake finished we ask the server for the message size
    # 2. Prepare Data
    buff_data = data_source 
    total_len = len(buff_data)
    
    #seq0: three-way handshake, seq1: ACK, Seq2: req_size
    seq_map = {2: 0} #<- data transfer: seq2 - seqN+2 (N= num of segmenations)
    next_seq = 2


    
    # 3. Transfer Data
    sliding_window(conn, state, buff_data, total_len, next_seq, seq_map)


def handle_stream(conn: socket.socket, state: ClientState):
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
                #if the state is closed - meaning four way handshake finished, we close the client app.
                with state.lock:
                    if state.state == "CLOSED":
                        break
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

def start_client(ip: str, port: int, state: ClientState, message: str):
    SERVER_ADDRESS = (ip, port)
    
    # 1. Create Shared State
    #state = ClientState()
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
        #dummy_file_data = "This is a long message that demonstrates the Go-Back-N sliding window protocol logic." * 5
        
        sender_thread = threading.Thread(
            target=sender_logic,
            args=(clientSocket, state, message),
            daemon=True
        )
        sender_thread.start()

        # 4. Initiate Handshake (Client Speaks First) [cite: 19]
        print(">>> Initiating Handshake (Sending SYN)...")
        state.state = "THREE_WAY_HANDSHAKE"
        syn_packet = {"flags": FLAG_SYN, "seq": 0, "file": state.file}
        clientSocket.sendall((json.dumps(syn_packet) + "\n").encode("utf-8"))
        
        # 5. Enter Receiver Loop (Main Thread)
        # This will block until connection closes
        handle_stream(clientSocket, state)

    except ConnectionRefusedError:
        print("Error: Could not connect. Is the server running?")
    except Exception as e:
        print(f"Client Error: {e}")
    finally:
        clientSocket.close()



def user_menu(ip: str, port: int):

    # Init
    state = ClientState()
    message = ""

    # Get inputs from user
    #he will choose message, window size and timeout value.
    print_options()
    option = input()

    # Process the choices
    if option == "1":
        # maximum_msg_size and dynamic_msg_size will be determined by the server
        message = input("enter message for the server: \n")
        try:
            state.window_size = int(input("choose window size - number of packets: \n"))
            if state.window_size <= 0:
                print("window_size > 0")
                raise ValueError
            state.timeout_value = int(input("enter the number of seconds for retransmission: \n"))
            if state.timeout_value <=2:
                print("timeout > 2")
                raise ValueError
        except ValueError:
            print("invalid parameters try again")
            user_menu(ip, port)

    #we will take values including maximum_msg_size from the config file
    elif option == "2":
        try:
            # finding the config Path
            CURRENT_DIR = pathlib.Path(__file__).resolve().parent
            CONFIG_PATH = CURRENT_DIR.parent / "config.txt"

            config_dict = {}
            with open(CONFIG_PATH, "r", encoding="utf-8") as config:  # open file with default closing
                for line in config:  # go over each line in the file
                    line = line.strip()  # should make the line empty in case of whitespace
                    if line:  # if line is not empty
                        try:
                            key, value = line.split(':', 1)  # split line by semicolon into key:value
                            key = key.strip()
                            value = value.strip()
                            # we set max split to 1 to make sure it does not
                            # Strip both standard quotes (") and curly quotes (” and “)
                            # as it is not part of the file name
                            value = value.strip('"').strip('”').strip('“')
                            config_dict[key] = value  # set the value of key
                        except ValueError:
                            print("invalid file format")  # if no semicolon, the format is not ok
            message_file = open(str(config_dict.get("message")))
            for line in message_file:
                message += line
            print(message)
            state.maximum_msg_size = int(config_dict.get("maximum_msg_size"))
            state.window_size = int(config_dict.get("window_size"))
            if state.window_size <= 0:
                print("window_size > 0")
                raise ValueError
            state.timeout_value = int(config_dict.get("timeout"))
            if state.timeout_value <= 2:
                print("timeout > 2")
                raise ValueError
            state.dynamic_message_size = config_dict.get("dynamic message size")
            state.file = True  # we need to update the server about config reading so he will read too
        except FileNotFoundError:
            print(f"Critical Error: Config file not found  \n OR: \n Message file was not found:")
        except ValueError:
            print("invalid input in the file, make sure it is like the skeleton provided in assignment 3")
            user_menu(ip, port)
    else:
        print("invalid input please choose 1 or 2")
        user_menu(ip, port)
    start_client(ip, port, state, message)


## -- JUST BASIC CLI INTERFACE --
def print_options():
    print("Please choose from the following options:")
    print("1 - work with user input")
    print("2 - work from file")


def main():
    ap = argparse.ArgumentParser(description="JSON TCP Client (MATALA3)")
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()
    user_menu(args.ip, args.port)
    #start_client(args.ip, args.port)

if __name__ == "__main__":
    main()