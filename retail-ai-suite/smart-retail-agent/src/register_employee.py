#!/usr/bin/env python3
# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Employee register service that subscribes to region count events and publishes
employee register messages when count reaches zero.
"""

import json
import logging
import os
import ssl
import sys
import time
import warnings
from datetime import datetime

import paho.mqtt.client as mqtt

# Suppress MQTT callback API deprecation warning
warnings.filterwarnings('ignore', category=DeprecationWarning, module='paho.mqtt.client')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MQTT Configuration
MQTT_HOST = os.getenv('MQTT_HOST', 'broker.scenescape.intel.com')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
MQTT_USE_TLS = os.getenv('MQTT_USE_TLS', 'false').lower() == 'true'
MQTT_CA_CERT_PATH = os.getenv('MQTT_CA_CERT_PATH', '../secrets/certs/scenescape-ca.pem')
MQTT_CERT_REQUIRED = os.getenv('MQTT_CERT_REQUIRED', 'true').lower() == 'true'
MQTT_VERIFY_HOSTNAME = os.getenv('MQTT_VERIFY_HOSTNAME', 'false').lower() == 'true'
MQTT_USERNAME = os.getenv('MQTT_USERNAME')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD')

SUBSCRIBE_TOPIC = 'scenescape/event/region/f87299dd-d9a3-4389-91ed-00aa071d23a1/57b67289-bfda-48f1-878a-26617a6dbda1/count'
PUBLISH_TOPIC = 'scenescape/data/sensor/Employee_Register'


class RegisterEmployeeService:
    """Service to listen for region count events and publish employee register messages."""
    
    def __init__(self):
        """Initialize the register employee service."""
        # Use MQTT client API version 1 (same as mqtt_service.py)
        self.client = mqtt.Client()
        
        # Enable automatic reconnection
        self.client.reconnect_delay_set(min_delay=1, max_delay=120)
        
        # Configure authentication if provided
        if MQTT_USERNAME and MQTT_PASSWORD:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            logger.info(f"MQTT authentication configured for user: {MQTT_USERNAME}")
        
        # Configure TLS if enabled
        if MQTT_USE_TLS:
            logger.info(f"Configuring MQTT TLS - CA Cert: {MQTT_CA_CERT_PATH}")
            try:
                # Resolve certificate path
                ca_cert_path = MQTT_CA_CERT_PATH
                if not os.path.isabs(ca_cert_path):
                    # Relative to app root
                    app_root = os.path.dirname(os.path.abspath(__file__))
                    ca_cert_path = os.path.join(app_root, ca_cert_path)
                
                if MQTT_CERT_REQUIRED:
                    if os.path.exists(ca_cert_path):
                        self.client.tls_set(
                            ca_certs=ca_cert_path,
                            certfile=None,
                            keyfile=None,
                            cert_reqs=ssl.CERT_REQUIRED,
                            tls_version=ssl.PROTOCOL_TLS
                        )
                        # Set hostname verification based on config (like singleton insecure flag)
                        if not MQTT_VERIFY_HOSTNAME:
                            self.client.tls_insecure_set(True)
                            logger.info("TLS configured with CA certificate (hostname verification disabled)")
                        else:
                            logger.info("TLS configured with CA certificate (full verification)")
                    else:
                        logger.warning(f"CA certificate not found: {ca_cert_path}")
                        raise FileNotFoundError(f"CA certificate not found: {ca_cert_path}")
                else:
                    # TLS without strict cert verification (like singleton insecure=True)
                    if os.path.exists(ca_cert_path):
                        self.client.tls_set(
                            ca_certs=ca_cert_path,
                            certfile=None,
                            keyfile=None,
                            cert_reqs=ssl.CERT_NONE,
                            tls_version=ssl.PROTOCOL_TLS
                        )
                    else:
                        self.client.tls_set(
                            ca_certs=None,
                            certfile=None,
                            keyfile=None,
                            cert_reqs=ssl.CERT_NONE,
                            tls_version=ssl.PROTOCOL_TLS
                        )
                    # Always disable hostname verification when cert not required
                    self.client.tls_insecure_set(True)
                    logger.info("TLS configured without strict verification (insecure)")
                    
            except Exception as e:
                logger.error(f"Failed to configure TLS: {e}")
                raise
        
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.connected = False
        
        logger.info(f"Register employee service initialized - Host: {MQTT_HOST}, Port: {MQTT_PORT}, TLS: {MQTT_USE_TLS}")
        logger.info(f"Subscribe topic: {SUBSCRIBE_TOPIC}")
        logger.info(f"Publish topic: {PUBLISH_TOPIC}")
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to MQTT broker (API VERSION1)."""
        if rc == 0:
            self.connected = True
            logger.info("Connected to MQTT broker successfully")
            
            # Subscribe to the count event topic
            result = client.subscribe(SUBSCRIBE_TOPIC)
            if result[0] == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Subscribed to topic: {SUBSCRIBE_TOPIC}")
            else:
                logger.error(f"Failed to subscribe to topic: {SUBSCRIBE_TOPIC}, error: {result}")
        else:
            self.connected = False
            error_messages = {
                1: "Connection refused - incorrect protocol version",
                2: "Connection refused - invalid client identifier",
                3: "Connection refused - server unavailable",
                4: "Connection refused - bad username or password",
                5: "Connection refused - not authorized",
                7: "Connection lost - network error"
            }
            error_msg = error_messages.get(rc, f"Unknown error code: {rc}")
            logger.error(f"Failed to connect to MQTT broker: {error_msg} (code: {rc})")
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected from MQTT broker (API VERSION1)."""
        self.connected = False
        if rc != 0:
            logger.warning(f"Unexpected disconnection from MQTT broker. Code: {rc}")
        else:
            logger.info("Disconnected from MQTT broker")
    
    def _on_message(self, client, userdata, msg):
        """
        Callback when a message is received.
        
        Args:
            client: MQTT client instance
            userdata: User data
            msg: MQTT message
        """
        try:
            # Parse the incoming message
            payload = json.loads(msg.payload.decode('utf-8'))
            
            # Get timestamp from the event message
            timestamp = payload.get('timestamp')
            if not timestamp:
                # If no timestamp in message, use current time
                timestamp = datetime.utcnow().isoformat() + 'Z'
            
            # Prepare the publish payload
            publish_payload = {
                "timestamp": timestamp,
                "id": "Employee_Register",
                "value": "employee"
            }
            
            # Publish the message
            result = client.publish(
                PUBLISH_TOPIC,
                json.dumps(publish_payload),
                qos=1
            )
            logger.info(f"published employee register message to {PUBLISH_TOPIC}")                
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Published message: {publish_payload}")
            else:
                logger.error(f"Failed to publish message. Return code: {result.rc}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message as JSON: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
    
    def connect(self):
        """Connect to the MQTT broker (following singleton.py pattern)."""
        try:
            logger.info(f"Connecting to MQTT broker at {MQTT_HOST}:{MQTT_PORT}...")
            self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            
            # Sleep to allow connection to establish (like singleton.py)
            time.sleep(2)
            
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            return False
    
    def run(self):
        """Start the register employee service with automatic reconnection."""
        logger.info("Starting MQTT service with automatic reconnection...")
        
        # Initial connection
        logger.info("Attempting to connect to MQTT broker...")
        if not self.connect():
            logger.error("Initial connection failed, exiting...")
            sys.exit(1)
        
        logger.info("Connection initiated, starting loop_forever...")
        
        try:
            # Start the blocking network loop - this will handle reconnection automatically
            self.client.loop_forever()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        finally:
            # Clean shutdown
            try:
                self.client.disconnect()
            except:
                pass
            logger.info("Register employee service stopped")


def main():
    """Main entry point."""
    logger.info("Starting Register Employee Service")
    
    service = RegisterEmployeeService()
    service.run()


if __name__ == "__main__":
    main()
