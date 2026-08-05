import React, { useEffect, useRef, useState } from 'react';

interface CanvasFeedProps {
  /** WebSocket path proxied by nginx to the pipeline's ws_video_server. */
  wsPath?: string;
}

/**
 * Low-latency live video via appsink → WebSocket → canvas.
 *
 * Connects to the pipeline's WebSocket (proxied at /ws/video), receives binary
 * JPEG frames, decodes each with createImageBitmap, and paints it to a <canvas>
 * — no polling, no <img> re-render. Used when the backend reports
 * `ui_video: true` (UI_VIDEO=1). Auto-reconnects if the stream drops.
 */
const CanvasFeed: React.FC<CanvasFeedProps> = ({ wsPath = '/ws/video' }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}${wsPath}`;
    let ws: WebSocket | null = null;
    let cancelled = false;

    const connect = () => {
      ws = new WebSocket(url);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) setTimeout(connect, 1000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = async (ev: MessageEvent) => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        try {
          const bmp = await createImageBitmap(
            new Blob([ev.data as ArrayBuffer], { type: 'image/jpeg' })
          );
          if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
            canvas.width = bmp.width;
            canvas.height = bmp.height;
          }
          const ctx = canvas.getContext('2d');
          ctx?.drawImage(bmp, 0, 0);
          bmp.close();
        } catch {
          /* drop a bad frame */
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      ws?.close();
    };
  }, [wsPath]);

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <canvas ref={canvasRef} style={{ display: 'block', width: '100%', background: '#000' }} />
      {!connected && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            background: 'rgba(0,0,0,0.55)',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            gap: 8,
          }}
        >
          <div className="det-video-spinner" />
          <span style={{ fontSize: 13, fontWeight: 500 }}>Connecting to live stream…</span>
        </div>
      )}
    </div>
  );
};

export default CanvasFeed;
