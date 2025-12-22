# import socket
from socket import *

#SERVER_ADDRESS = ('localhost', 13000)
serverName = 'localhost'
serverPort = 13000
SERVER_ADDRESS = (serverName, serverPort)

# clientSocket = socket.socket()
clientSocket = socket(AF_INET, SOCK_STREAM)
clientSocket.connect(SERVER_ADDRESS)
sentence = input('Input lowercase sentence:')

# clientSocket.send(bytes(sentence, encoding="UTF-8"))
clientSocket.send(sentence.encode())

modifiedSentence = clientSocket.recv(4096)
# print('From Server:', modifiedSentence.decode("UTF-8"))
print('From Server:', modifiedSentence.decode())

clientSocket.close()



def main():
    print(f"starting the shitty Server")


if __name__ == "__main__":
    main()