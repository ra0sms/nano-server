#!/bin/bash

# Чтение IP-адреса из файла
IP_FILE="/home/pi/nano-server/client_ip.cfg"
if [[ ! -f "$IP_FILE" ]]; then
  echo "File was not found: $IP_FILE"
  exit 1
fi

IP_ADDRESS=$(cat "$IP_FILE" | tr -d '\n' | tr -d ' ')
if [[ -z "$IP_ADDRESS" ]]; then
  echo "IP address was not found: $IP_FILE"
  exit 1
fi

# Путь к файлу конфигурации аудио
CONFIG_FILE="/home/pi/nano-server/audio/audio_config.cfg"

# Значения по умолчанию
DEFAULT_RATE=48000
DEFAULT_BUFFER_TIME=100000

# Чтение настроек из файла конфигурации
if [ -f "$CONFIG_FILE" ]; then
    # Читаем RATE (частота)
    RATE=$(grep "^RATE=" "$CONFIG_FILE" | cut -d'=' -f2)
    RATE=${RATE:-$DEFAULT_RATE}

    # Читаем BUFFER_TIME (время буфера в микросекундах)
    # В конфиге хранится значение LATENCY, которое мы используем как buffer-time
    BUFFER_TIME=$(grep "^LATENCY=" "$CONFIG_FILE" | cut -d'=' -f2)

    if [[ -z "$BUFFER_TIME" ]]; then
        BUFFER_TIME=$DEFAULT_BUFFER_TIME
    fi
else
    RATE=$DEFAULT_RATE
    BUFFER_TIME=$DEFAULT_BUFFER_TIME
fi

echo "Starting Audio SERVER:"
echo "  Target IP: $IP_ADDRESS"
echo "  Sample Rate: $RATE Hz"
echo "  Buffer Time: $BUFFER_TIME us"

gst-launch-1.0 alsasrc device=hw:1 buffer-time=$BUFFER_TIME latency-time=1000 ! \
audioconvert ! audioresample ! \
capsfilter caps="audio/x-raw,rate=$RATE,channels=1,format=S16LE" ! \
equalizer-3bands band0=-24 band1=0 band2=0 ! \
audioconvert ! \
opusenc bitrate=48000 bitrate-type=vbr frame-size=20 complexity=2 ! rtpopuspay ! \
udpsink host=$IP_ADDRESS port=5000 sync=false
