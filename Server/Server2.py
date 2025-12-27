import argparse
import json
import os
import pathlib
import socket
import threading
import random # [Added] Needed for dynamic resizing logic
import time

# --- TCP FLAG CONSTANTS ---
FLAG_FIN = 0x01
FLAG_SYN = 0x02
FLAG_RST = 0x04
FLAG_PSH = 0x08
FLAG_ACK = 0x10

# --- SERVER CONFIG ---
SERVER_CONFIG = {
    "max_msg_size": 20,   # Initial default
    "dynamic_window": True # Enable the dynamic behavior 
}

class ConnectionState:
    """Tracks sequence numbers and buffer for ONE client"""
    def __init__(self):
        self.state = "LISTEN"
        self.expected_seq = 0
        self.buffer = {}
        # Track the current limit for this specific connection
        self.current_max_msg_size = SERVER_CONFIG["max_msg_size"]
        self.message = ""

def handle_packets(packet, state: ConnectionState):
    """
    The Brain: Processes the packet using Bit-Flags and updates State.
    """
    # set the flags according to what we got in the packet.
    # If there is no flags section in the decoded packet, we set it to 0 (meaning no flag is active),
    # this should only happen if the packet is not ok
    flags = packet.get("flags", 0)
    #set the sequence number according to what we got in the packet.
    #If there is no seq section in the decoded packet, we set it to 0 (default before the handshake finished),
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
            #we check if file is set to true by the client to read the config file
            payload = packet.get
            if packet.get("file") is True:
                print(f"[Server] reading settings from file")
                #finding config path
                CURRENT_DIR = pathlib.Path(__file__).resolve().parent
                CONFIG_PATH = CURRENT_DIR.parent / "config.txt"
                config_dict = {}
                with open(CONFIG_PATH, "r", encoding="utf-8") as config:  # open file with default closing
                    for line in config:  # go over each line in the file
                        line = line.strip()  # should make the line empty in case of whitespace
                        if line:  # if line is not empty
                            try:
                                key, value = line.split(':',1)  # split line by semicolon into key:value
                                key = key.strip()
                                value = value.strip()
                                #we set maxsplit to 1 to make sure it does not
                                #Strip both standard quotes (") and curly quotes (” and “)
                                # as it is not part of the file name
                                value = value.strip('"').strip('”').strip('“')
                                config_dict[key] = value  # set the value of key
                            except ValueError:
                                print("invalid file format")  # if no semicolon, the format is not ok
                try:
                    state.current_max_msg_size = int(config_dict.get("maximum_msg_size")) #we set max message size from file
                    SERVER_CONFIG["dynamic_window"] = config_dict.get("dynamic_message_size") #we set dynamic_window from the file
                except ValueError:
                    print("invalid values in the file")  # either max_msg_size is not in or dynamic message size is not bool
            return {#either way we return the syn ack packet with the max message size and dynamic window from server
                "flags": FLAG_SYN | FLAG_ACK, 
                "ack": 0,
                "max_msg_size": state.current_max_msg_size,
                "dynamic_window": SERVER_CONFIG["dynamic_window"],
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
            #we check if we need to send the size of the message as part of the process:
            if payload == "REQ_SIZE":
                print(f"[Server] Received Size Request (Seq {seq_num}). Sending: {state.current_max_msg_size}")
                # We acknowledge the request and send the size
                # The ACK increments by one
                state.expected_seq += 1
                return {
                    "flags": FLAG_ACK | FLAG_PSH,
                    "ack": seq_num,
                    "max_msg_size": state.current_max_msg_size,
                    "payload": f"The requested size was: {state.current_max_msg_size}"  # Optional acknowledgement text
                }
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
                state.message += packet.get("payload")
                
                # Check Buffer for gaps we can now fill 
                while state.expected_seq in state.buffer: 
                    print(f"   -> Buffer Fill Seq {state.expected_seq}")
                    del state.buffer[state.expected_seq]
                    state.expected_seq += 1
                    state.message += packet.get("payload")
                #if we have later packets in the buffer, we need to check if we got the fin already from the client
                if state.expected_seq in state.buffer:
                    buffered_packet = state.buffer[state.expected_seq]
                    if buffered_packet.get("flags", 0) & FLAG_FIN:
                        print(f"[Server] Found buffered FIN (Seq {state.expected_seq}). All data now complete.")
                        time.sleep(1.0)
                        state.state = "LAST_ACK"
                        return {"flags": FLAG_ACK, "ack": state.expected_seq + 1}
                
                
                # --- [ADDED] DYNAMIC RESIZING LOGIC ---
                # "The server can also send the flag 'dynamic message size = true'"
                response = {"flags": FLAG_ACK, "ack": state.expected_seq - 1}
                print(f"dynamic_window: {SERVER_CONFIG["dynamic_window"]}")
                if SERVER_CONFIG["dynamic_window"] == str(True):
                    new_size = state.current_max_msg_size
                    if SERVER_CONFIG["dynamic_window"] and random.random() < 0.2: # 20% chance to change
                        # 3:1 Bias: 75% chance to grow/stay, 25% chance to shrink
                        change_factor = random.choices([1.5, 0.5], weights=[0.75, 0.25])[0]
                        new_size = int(state.current_max_msg_size * change_factor)
                        new_size = max(10, min(new_size, 1024)) # Clamp values
                    
                        if new_size != state.current_max_msg_size:
                            print(f"[Dynamic] Resizing Max Msg to {new_size}")
                            state.current_max_msg_size = new_size
                    response["max_msg_size"] = new_size  # Client will see this and wipe map
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
            #if we have processed all packets we can send fin from server side.
            if seq_num == state.expected_seq:
                print(f"[Server] Received FIN (Seq {seq_num}). All data received. Sending ACK.")
                state.state = "LAST_ACK"
                return {"flags": FLAG_ACK | FLAG_FIN, "ack": seq_num + 1}
            #if we have not finished processing packets we are waiting for finishing processing first,
            # we buffer the fin for late
            elif seq_num > state.expected_seq:
                print(
                    f"[Server] Early FIN received (Seq {seq_num}). Waiting for missing data (Expect {state.expected_seq}).")
                # We are missing packets!
                # We Buffer this FIN packet just like out-of-order data.
                # When the missing data arrives, we will check the buffer and find this FIN.
                state.buffer[seq_num] = packet
                return {"flags": FLAG_ACK, "ack": state.expected_seq - 1}

            else:
                # Duplicate FIN (Old packet), just ACK it again
                return {"flags": FLAG_ACK, "ack": state.expected_seq}
    elif state.state == "LAST_ACK":
        if flags & FLAG_ACK:
            #if we received the ack for our fin, we can close the connection with the client
            #because server is usually always on, I changed the state for listen, to wait for a new client
            print(f"[Server] the message is: {state.message}")
            print("[Server] Received Final ACK. Connection Finished. now we wait for a new connection")

            state.state = "LISTEN"

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