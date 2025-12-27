# import socket
import argparse
import json
import socket
import threading
import time
import pathlib


# --- NAMING CONVENTIONS & FLAGS ---
# These flags mimic the standard TCP header bits.
FLAG_FIN = 0x01 # "Finish". Used to gracefully close the connection.
FLAG_SYN = 0x02 # "Synchronize". 3-way handshake.
FLAG_PSH = 0x08 # "Push". (Payload).
FLAG_ACK = 0x10 # "Acknowledgment". 

# Default values are being changed during runtime by user / config
CLIENT_CONFIG = {
    "server_ip": "127.0.0.1", # Default
    "server_port": 13000,     # Default
    "timeout": 5.0,        # (Seconds) - being changed during runtime by user / config
    "window_size": 0       # (Sliding Window parrallel packets) - being changed during runtime by user / config
}

# Client's state class of the Connection (easier to debug with a class)
class ClientState:
    def __init__(self):
        self.lock = threading.Lock() # ignore (done to avoid parrelism bugs)

        # States: CLOSED -> THREE_WAY_HANDSHAKE -> REQ_SIZE -> DATA_TRANSFER -> WAIT_FOR_FIN_ACK -> FIN_ACK
        self.state = "CLOSED"  
        
        self.seq_num = 0         # Client's current sequence number (for Handshake)
        self.window_base = 0     # The oldest unacknowledged packet sequence number
        self.max_msg_size = 1024 # Default size, updated dynamically by Server 
        self.window_size = CLIENT_CONFIG["window_size"] # Sliding window capacity, updated by config / user input
        
        self.dynamic_message_size = False # Updated during 
        
        # Timer Logic
        self.timer_start = None
        self.timeout_value = CLIENT_CONFIG["timeout"]
        self.file = False


""" BASIC MAIN """
def main():
    ap = argparse.ArgumentParser(description="JSON TCP Client (MATALA3)")
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()
    user_menu(args.ip, args.port)
    #start_client(args.ip, args.port)

if __name__ == "__main__":
    main()

## -- JUST BASIC CLI INTERFACE --
def print_options():
    print("Please choose from the following options:")
    print("1 - work with user input")
    print("2 - work from file")

def user_menu(ip: str, port: int):

    # Init
    state = ClientState()
    message = ""

    # Get inputs from user
    #user will choose message, window size and timeout value.
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
                            # we set maxsplit to 1 to make sure it does not
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
        except FileNotFoundError:
            print(f"Critical Error: Config file not found at {CONFIG_PATH} \n OR: \n Message was not found: {message_file}")
        
        try:
            state.maximum_msg_size = int(config_dict.get("maximum_msg_size"))
            state.window_size = int(config_dict.get("window_size"))
            state.timeout = int(config_dict.get("timeout"))
            state.dynamic_message_size = bool(config_dict.get("dynamic message size"))
            state.file = True #we need to update the server about config reading so he will read too
        except ValueError:
            print("invalid input in the file, make sure it is like the skeleton provided in assigment 3")
            user_menu(ip, port)
    else:
        print("invalid input please choose one or 2")
        user_menu(ip, port)
    start_client(ip, port, state, message)

## -- TCP SERVER INIT --
## We start 2 Threads, one for the real TCP under handle_stream
## another thread, will be for the TCP we emulate. (MATALA logic)
def start_client(ip: str, port: int, state: ClientState, message: str):
    SERVER_ADDRESS = (ip, port)
    
    # 1. Connect Socket
    clientSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    clientSocket.settimeout(None) # Blocking mode for recv
    
    try:
        clientSocket.connect(SERVER_ADDRESS)
        print(f"Connected to {ip}:{port}")
        
        # 2. Start Sender Thread
        sender_thread = threading.Thread(
            target=TCP_emulator, # <-- Will be sending the emulated packets
            args=(clientSocket, state, message),
            daemon=True
        )
        sender_thread.start()
        
        # 3. Real TCP Stream Thread (Main Thread)
        handle_stream(clientSocket, SERVER_ADDRESS, state) # This will block until connection closes
    except ConnectionRefusedError:
        print("Error: Could not connect. Is the server running?")
    except Exception as e:
        print(f"Client Error: {e}")
    finally:
        clientSocket.close()


def TCP_emulator(conn: socket.socket, state: ClientState, data_source: str):
    """
    Main Sender Loop: Orchestrates the connection lifecycle.
    """
    # 1. Handshake
    three_way_handshake(conn ,state)
    #we ask after handshake for initial message size
    ask_size(conn, state)
    print("[Sender] Connection REQ_SIZE. Starting Dynamic Data Transfer...")
    #after the handshake finished we ask the server for the message size
    
    # 2. Prepare Data
    buff_data = data_source 
    total_len = len(buff_data)

    #seq0: three-way handshake, seq1: request message size
    seq_map = {2: 0} #<- data transfer: seq2 - seqN+2 (N= num of segmenations)
    next_seq = 2
    
    # 3. Transfer Data
    sliding_window(conn, state, buff_data, total_len, next_seq, seq_map)


def handle_stream(clientSocket, SERVER_ADDRESS, state):
    pass


def handle_packets(packet, state):
    """
    Client Logic: Reacts to Server Packets and updates State (Window, MaxSize).
    """
    flags = packet.get("flags", 0)
    server_ack = packet.get("ack", 0)
    server_seq = packet.get("seq", 0)
    # LOGS: "[RECV] Flags={flags}, Ack={server_ack}"
    #       "[Sender] Sending Seq {server_seq} (Size: {})""

    # --- 1. HANDLE HANDSHAKE (Server sent SYN-ACK) ---
    # TODO: Make sure if we don't get from the Server back and SYN-ACK, we send after timeout a SYN again.
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
                state.seq_num += 1
                # Handshake consumes Seq 0. Window starts at Seq 1.
                state.window_base = 1 
            
            # Respond with ACK
            return {"flags": FLAG_ACK, "seq": state.seq_num, "ack": 0}
        elif state.timer_start is not None:
                elapsed = time.time() - state.timer_start
                if elapsed > state.timeout_value:
                    print(f"[!!!] TIMEOUT ({elapsed:.2f}s)! Resending Syn packet")
    #if connection is REQ_SIZE we need to ask for initial message size
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
    if state.state in ["Wait_for_FIN_ACK", "FIN_ACK"]:#TODO check if we can do it without wait state
        if flags & FLAG_FIN:
            print("   >>> [Recv] Step 3: Server sent FIN.")

            # Step 4: Send Final ACK
            ack_to_send = server_seq + 1

            with state.lock:
                state.state = "CLOSED"

            print(f"   >>> [Send] Step 4: Sending Final ACK ({ack_to_send}). Connection CLOSED.")
            return {"flags": FLAG_ACK, "seq": state.seq_num, "ack": ack_to_send}

    return None

