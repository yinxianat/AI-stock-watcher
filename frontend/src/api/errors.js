// Maps HTTP status codes to readable names. 0 means the request never got a
// response (offline, DNS failure, CORS block).
export const STATUS_NAME = Object.freeze({
  0: "NetworkError",
  400: "BadRequestError",
  401: "UnauthorizedError",
  403: "ForbiddenError",
  404: "NotFoundError",
  408: "RequestTimeoutError",
  409: "ConflictError",
  422: "ValidationError",
  429: "RateLimitError",
  500: "ServerError",
  502: "BadGatewayError",
  503: "ServiceUnavailableError",
  504: "GatewayTimeoutError",
});

// Falls back to the status class for codes not listed above (e.g. 418 → ClientError).
export function statusName(status) {
  if (status in STATUS_NAME) return STATUS_NAME[status];
  if (status >= 500) return "ServerError";
  if (status >= 400) return "ClientError";
  return "UnknownError";
}

export class ApiError extends Error {
  constructor(message, { status, detail, path, method, cause } = {}) {
    super(message);
    this.name =
      STATUS_NAME[status] ?? (status ? `ApiError(${status})` : "ApiError");
    this.status = status;
    this.detail = detail;
    this.path = path;
    this.method = method;
    this.cause = cause;
  }

  get isRetryable() {
    const safe =
      !this.method ||
      ["GET", "HEAD", "PUT", "DELETE"].includes(this.method.toUpperCase());
    return safe && ([0, 408, 429].includes(this.status) || this.status >= 500);
  }

  toLogErrorPayload() {
    return {
      name: this.name,
      message: this.message,
      status: this.status ?? null,
      path: this.path ?? null,
      method: this.method ?? null,
    };
  }
}

export class ValidationError extends ApiError {
  constructor(message, { detail, path, method } = {}) {
    const defaultMessage =
      "The information you entered isn't valid. Please check and try again.";
    super(defaultMessage, { status: 422, path, method });
    this.name = "ValidationError";
    this.serverMessage = message;
    this.logDetail = detail;
  }

  get userMessage() {
    return this.serverMessage || this.message;
  }
}

// Human-readable wait, e.g. 45 → "45 seconds", 1313 → "22 minutes".
export function formatWait(seconds) {
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

// Short countdown for buttons, e.g. 45 → "45s", 1313 → "21:53".
export function formatCountdown(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return `${m}:${s}`;
}

export class RateLimitError extends ApiError {
  constructor(message, { status = 429, detail, path, method, retryAfterSeconds = null } = {}) {
    super(message, { status, detail, path, method });
    this.name = "RateLimitError";
    // From the Retry-After header; null if the server didn't say.
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

// The request never got a response (offline, DNS failure, CORS block).
// `cause` is the original error thrown by fetch, kept for debugging.
export class NetworkError extends ApiError {
  constructor(cause, { path, method } = {}) {
    const message = "Could not reach the server. Check your connection.";
    super(message, { status: 0, path, method, cause });
    this.name = "NetworkError";
  }
}

// The request took longer than the client-side limit, so the browser gave up.
// Uses 408 (Request Timeout) so isRetryable and STATUS_NAME treat it like one,
// even though the server never actually sent a 408.
export class TimeoutError extends ApiError {
  constructor(cause, { path, method, timeoutMs } = {}) {
    const message = "The request took too long. Please try again.";
    super(message, { status: 408, path, method, cause });
    this.name = "TimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

export function errorFromResponse(res, data, { path, method } = {}) {
  const errorStatus = res?.status;
  const detail = data?.detail;
  let message = getMessageFrom(detail, res.statusText);
  let errorObj;
  switch (errorStatus) {
    case 422:
      errorObj = new ValidationError(message, { detail, path, method });
      break;
    case 429: {
      const raw = res.headers.get("Retry-After");
      const seconds = raw && /^\d+$/.test(raw) ? Number(raw) : null;
      message = seconds
        ? `Too many requests. Try again in ${formatWait(seconds)}.`
        : "Too many requests, please wait a moment";
      errorObj = new RateLimitError(message, {
        status: errorStatus, detail, path, method, retryAfterSeconds: seconds,
      });
      break;
    }
    default:
      errorObj = new ApiError(message, { status: errorStatus, detail, path, method });
  }
  return errorObj;
}

export function getMessageFrom(detail, statusText){
    if (typeof detail === "string" && detail.trim()) return detail;
    if(Array.isArray(detail) && detail.length>0){
        return detail[0]?.msg || "Invalid value"
    }
    return statusText || "Request failed";
}