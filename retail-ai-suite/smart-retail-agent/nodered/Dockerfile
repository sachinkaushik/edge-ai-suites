FROM nodered/node-red:latest

# Install dashboard nodes
RUN cd /data && npm install node-red-dashboard

# Copy flows and settings
COPY flows.json /data/flows.json
COPY settings.js /data/settings.js
