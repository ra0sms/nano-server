#!/bin/bash

# Auto-detect C-Media USB Audio card number
CARD_NUM=$(grep -E "C-Media|USB Audio" /proc/asound/cards | grep -v webcam | awk '{print $1}')
if [[ -z "$CARD_NUM" ]]; then
    CARD_NUM=$(grep -vE "audiocodec|webcam" /proc/asound/cards | grep -v "^$" | head -1 | awk '{print $1}')
fi
if [[ -z "$CARD_NUM" ]]; then
    CARD_NUM=0
fi

echo "Audio Client: using ALSA card $CARD_NUM"

# Audio settings
/usr/bin/amixer -c "$CARD_NUM" cset numid=3 31

# Audio stream receive
gst-launch-1.0 udpsrc port=5000 caps="application/x-rtp,payload=96" ! \
rtpjitterbuffer latency=100 drop-on-latency=false ! \
queue max-size-time=50000000 leaky=downstream ! \
rtpopusdepay ! opusdec plc=true ! queue ! audioconvert ! queue ! \
alsasink device=hw:$CARD_NUM buffer-time=100000 latency-time=1000 sync=false
