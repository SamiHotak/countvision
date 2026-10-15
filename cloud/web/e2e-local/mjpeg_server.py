"""Test camera for the CountVision Local browser test: serves a video file as an endless MJPEG stream
(like a phone running an "IP Webcam" app) at the video's own frame rate.

    python mjpeg_server.py demo_scene.avi 8090      ->  http://127.0.0.1:8090/video
"""

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

VIDEO, PORT = sys.argv[1], int(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        cap = cv2.VideoCapture(VIDEO)
        fps = cap.get(cv2.CAP_PROP_FPS) or 15
        due = time.time()
        try:
            while True:
                ok, img = cap.read()
                if not ok:  # loop the video
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                ok, jpg = cv2.imencode(".jpg", img)
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                                 + jpg.tobytes() + b"\r\n")
                due += 1 / fps
                time.sleep(max(0.0, due - time.time()))
        except (BrokenPipeError, ConnectionResetError):
            pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
