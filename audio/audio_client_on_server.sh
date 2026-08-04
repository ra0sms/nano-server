#!/bin/bash

# Audio settings
#/usr/bin/amixer -c 0 cset numid=3 31

# Audio stream receive
gst-launch-1.0 udpsrc port=5000 caps="application/x-rtp,payload=96" ! \
rtpjitterbuffer latency=100 drop-on-latency=false ! \
queue max-size-time=50000000 leaky=downstream ! \
rtpopusdepay ! opusdec plc=true ! audioconvert ! \
audiochebband mode=band-reject lower-frequency=45 upper-frequency=55 poles=8 ! \
audiocheblimit mode=high-pass cutoff=100 poles=8 ! \
queue ! audioconvert ! queue ! \
alsasink device=hw:0 buffer-time=100000 latency-time=1000 sync=false
