import type { Health } from "@/components/ui/status-dot";
import type { Camera, Device } from "@/lib/api";

export function deviceHealth(d: Device): { health: Health; label: string } {
  if (d.revoked) return { health: "idle", label: "Removed" };
  if (!d.online) return { health: "down", label: d.last_seen_at ? "Offline" : "Waiting for first contact" };
  if (d.camera_count > 0 && d.cameras_online < d.camera_count) return { health: "warn", label: "Online, camera problem" };
  return { health: "ok", label: "Online" };
}

export function cameraHealth(c: Camera, deviceOnline: boolean): { health: Health; label: string } {
  if (!deviceOnline) return { health: "idle", label: "Device offline" };
  if (c.alert) return { health: "down", label: c.alert };
  switch (c.state) {
    case "running":
      return c.connected === false ? { health: "warn", label: "Reconnecting" } : { health: "ok", label: "Counting" };
    case "connecting":
    case "starting":
      return { health: "warn", label: "Connecting" };
    case "offline":
    case "restarting":
      return { health: "warn", label: "Reconnecting" };
    case "failed":
      return { health: "down", label: "Failed" };
    case "paused":
      return { health: "idle", label: "Paused outside counting hours" };
    case "finished":
      return { health: "idle", label: "Video finished" };
    default:
      return { health: "idle", label: c.state ?? "Unknown" };
  }
}
