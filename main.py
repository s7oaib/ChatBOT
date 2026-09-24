import os
import socket
import threading
import uvicorn

def start_tcp_bridge(src_port: int, target_port: int):
    """Binds to src_port and forwards all raw TCP traffic to target_port."""
    if src_port == target_port:
        return

    def handle_client(client_sock):
        try:
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.connect(("127.0.0.1", target_port))
            
            def pipe(s1, s2):
                try:
                    while True:
                        data = s1.recv(4096)
                        if not data:
                            break
                        s2.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        s1.close()
                    except Exception:
                        pass
                    try:
                        s2.close()
                    except Exception:
                        pass
            
            t1 = threading.Thread(target=pipe, args=(client_sock, target_sock), daemon=True)
            t2 = threading.Thread(target=pipe, args=(target_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def server_loop():
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", src_port))
            server.listen(128)
            print(f"Dual-port bridge active: forwarding port {src_port} -> {target_port}")
            while True:
                client, _ = server.accept()
                threading.Thread(target=handle_client, args=(client,), daemon=True).start()
        except Exception:
            # Port might already be in use or bound
            pass

    t = threading.Thread(target=server_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    raw_port = os.environ.get("PORT", "8080")
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        port = 8080
    
    # If app runs on 8080, also bridge 8000 -> 8080 (and vice versa)
    alternate_port = 8000 if port == 8080 else 8080
    start_tcp_bridge(alternate_port, port)

    print(f"Starting Camellia Artisanal Bot on 0.0.0.0:{port} (and bridging {alternate_port})...")
    uvicorn.run(
        "camellia.app:app",
        host="0.0.0.0",
        port=port,
        # Meta's GET verification token is a query parameter. Uvicorn's
        # access log records full URLs, including that credential.
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
