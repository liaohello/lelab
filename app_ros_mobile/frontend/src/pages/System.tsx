import SystemStorage from "./SystemStorage";

/**
 * `/system` — Jetson runtime dashboard (CPU/GPU/RAM/temp/disk/devices).
 *
 * Shares its data layer with `/storage` so both views reuse one set of
 * WebSocket subscriptions and React Query caches.
 */
const System = () => <SystemStorage view="system" />;

export default System;
