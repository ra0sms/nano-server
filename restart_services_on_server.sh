#!/bin/bash
systemctl restart audio_server.service
systemctl restart audio_client_on_server.service
systemctl restart mjpeg-streamer.service
systemctl restart ptt_server.service
