import unittest
import socket
import json
import threading
import time
import sys
import os
from unittest.mock import patch, mock_open

# --- PATH SETUP ---
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, '..'))
server_dir = os.path.join(root_dir, 'Server')
client_dir = os.path.join(root_dir, 'Client')

sys.path.append(root_dir)
sys.path.append(server_dir)
sys.path.append(client_dir)

# --- IMPORTS ---
try:
    from Server2 import start_server, SERVER_CONFIG
    from Client2 import ClientState
    # Try importing parser to test it, but don't crash if missing yet
    try:
        from FileInterpeter import parse_config_file
    except ImportError:
        parse_config_file = None
except ImportError as e:
    print(f"CRITICAL ERROR: {e}")
    exit(1)

TEST_IP = "127.0.0.1"
TEST_PORT = 15000
FLAG_SYN = 0x02
FLAG_ACK = 0x10
FLAG_PSH = 0x08

class TestAssignment3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server once for all tests
        cls.server_thread = threading.Thread(target=start_server, args=(TEST_IP, TEST_PORT), daemon=True)
        cls.server_thread.start()
        time.sleep(1)

    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect((TEST_IP, TEST_PORT))

    def tearDown(self):
        self.sock.close()

    def send_json(self, data):
        self.sock.sendall((json.dumps(data) + "\n").encode("utf-8"))

    def recv_json(self):
        try:
            data = b""
            while b"\n" not in data:
                chunk = self.sock.recv(1024)
                if not chunk: break
                data += chunk
            line, _, _ = data.partition(b"\n")
            return json.loads(line.decode("utf-8")) if line else None
        except socket.timeout:
            return None

    # =========================================================================
    # PART 1: LOGIC THAT SHOULD PASS NOW
    # =========================================================================

    def test_01_handshake_syn_ack(self):
        """TC01: Verify Server2 responds to SYN with SYN-ACK."""
        self.send_json({"flags": FLAG_SYN, "seq": 0})
        resp = self.recv_json()
        self.assertIsNotNone(resp, "Server2 dropped SYN packet")
        self.assertTrue(resp["flags"] & FLAG_SYN)
        self.assertTrue(resp["flags"] & FLAG_ACK)

    def test_02_data_transfer_ack(self):
        """TC02: Verify Server2 ACKs data packets (ACK = Last Received ID)."""
        # 1. Handshake
        self.send_json({"flags": FLAG_SYN, "seq": 0})
        self.recv_json()
        self.send_json({"flags": FLAG_ACK, "seq": 1})
        
        # 2. Send Data M1 (Seq 1)
        self.send_json({"flags": FLAG_PSH, "seq": 1, "payload": "Test"})
        resp = self.recv_json()
        
        # LOGIC FIX: Assignment says "If server receives M0... sent ACK0" [cite: 54]
        # We sent M1 (Seq 1), so we expect ACK 1.
        self.assertEqual(resp["ack"], 1, "Server should ACK the seq number of the received packet")


    def test_05_buffer_drain_logic(self):
        """TC05: [Logic] Server must ACK cumulative seq when gap is filled."""
        # Handshake
        self.send_json({"flags": FLAG_SYN, "seq": 0})
        self.recv_json()
        self.send_json({"flags": FLAG_ACK, "seq": 1}) # Server Expects 1

        # 1. Send Seq 1 (Good) -> Expect ACK 1
        self.send_json({"flags": FLAG_PSH, "seq": 1, "payload": "A"})
        self.assertEqual(self.recv_json()["ack"], 1)

        # 2. Send Seq 3 (Gap! Skip 2) -> Expect ACK 1 (Duplicate of last valid)
        self.send_json({"flags": FLAG_PSH, "seq": 3, "payload": "C"})
        self.assertEqual(self.recv_json()["ack"], 1)

        # 3. Send Seq 2 (Fill Gap) -> Expect ACK 3
        # Logic: Server now has 1, 2, 3. Highest contiguous is 3.
        self.send_json({"flags": FLAG_PSH, "seq": 2, "payload": "B"})
        resp = self.recv_json()
        self.assertEqual(resp["ack"], 3, "Should ACK 3 after filling gap (processed 2 and 3)")

    # =========================================================================
    # PART 2: FEATURES YOU NEED TO IMPLEMENT (Expect FAIL)
    # =========================================================================

    def test_03_input_file_parsing(self):
        """TC03: [PDF req] Check if parse_config_file works."""
        if parse_config_file is None:
            self.fail("Function 'parse_config_file' is missing! Implement it in FileInterpeter.py")

        # Mocking a file to test your parser
        mock_content = 'message:"file.txt"\nmaximum_msg_size:500\nwindow_size:5\ntimeout:3\ndynamic message size:True'
        with patch("builtins.open", mock_open(read_data=mock_content)):
            config = parse_config_file("dummy.txt")
            
            self.assertEqual(config.get("max_msg_size"), 500)
            self.assertTrue(config.get("dynamic_window"))
            # Check for path quote removal
            path = config.get("file_path", "")
            self.assertFalse('"' in path, "Parser should remove quotes from file path")

    def test_04_dynamic_size_always_present(self):
        """TC04: [PDF req] Dynamic Size must be in EVERY ACK if enabled."""
        # Force Enable Dynamic Window for this test
        SERVER_CONFIG["dynamic_window"] = True 
        
        # Handshake
        self.send_json({"flags": FLAG_SYN, "seq": 0})
        self.recv_json()
        self.send_json({"flags": FLAG_ACK, "seq": 1})

        # Send packets. Check ACKs.
        for i in range(1, 4):
            self.send_json({"flags": FLAG_PSH, "seq": i, "payload": "data"})
            resp = self.recv_json()
            # Fails if you haven't added logic to always send max_msg_size
            self.assertIn("max_msg_size", resp, f"ACK {i} missing 'max_msg_size'. PDF requires it in EVERY ACK.")
if __name__ == '__main__':
    unittest.main()