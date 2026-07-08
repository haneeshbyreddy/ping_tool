// Set when a signed-in session dies on a 401 (use-auth), read once by the
// login page so it can say "session expired" instead of a cold sign-in form.
export const SESSION_EXPIRED_KEY = "wisp:session-expired"
