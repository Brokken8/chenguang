export interface CheckinIdentity {
  name: string;
  studentId: string;
}

const SESSION_KEY = "morning_checkin_identity";
export const STUDENT_ID_PATTERN = /^U202\d{6}$/;

export function isValidIdentity(value: unknown): value is CheckinIdentity {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<CheckinIdentity>;
  return typeof candidate.name === "string" && Boolean(candidate.name.trim()) &&
    typeof candidate.studentId === "string" && STUDENT_ID_PATTERN.test(candidate.studentId);
}

export function readIdentity(): CheckinIdentity | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    return isValidIdentity(value) ? value : null;
  } catch {
    return null;
  }
}

export function saveIdentity(identity: CheckinIdentity): void {
  if (!isValidIdentity(identity)) throw new Error("请填写姓名和有效的学号");
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(identity));
  // Keep the existing form's remembered identity compatible with the new entrance.
  try {
    localStorage.setItem("morning_checkin_name", identity.name);
    localStorage.setItem("morning_checkin_student_id", identity.studentId);
  } catch {
    // Remembering on this device is optional; the current session still works.
  }
}

export function readRememberedIdentity(): CheckinIdentity {
  const active = readIdentity();
  if (active) return active;
  try {
    return {
      name: localStorage.getItem("morning_checkin_name") || "",
      studentId: localStorage.getItem("morning_checkin_student_id") || "",
    };
  } catch {
    return { name: "", studentId: "" };
  }
}
