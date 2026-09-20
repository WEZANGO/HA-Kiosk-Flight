FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.2.0" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

# Stdlib only: every aircraft position is drawn in the browser from polar
# coordinates the app computed, so there is no tile server, no SDK and no image
# to render server-side (that is what keeps this app usable on a VLAN with no
# internet and small on armv7/armhf/i386).
RUN apk add --no-cache python3

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]