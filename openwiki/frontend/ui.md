# Frontend UI notes

The frontend is a React/Vite single-page app in [`apps/frontend/src/App.tsx`](../apps/frontend/src/App.tsx) with companion styles in [`apps/frontend/src/styles.css`](../apps/frontend/src/styles.css). It is mostly presentation logic plus API orchestration.

## Main user journeys

### Registration flow
The public-facing screen collects:
- a person name
- an email address
- a password
- an eight-character registration code in the format `AAAA-0000`

The UI intentionally uses segmented registration-code inputs and a generated-password helper so users do not need to type the code in one field or invent weak passwords. The code field pattern is enforced both in the client and the backend.

### Administrator workspace
After login, the admin area provides:
- a session-gated dashboard
- search across lab users
- a create-user form
- a delete confirmation flow
- links to the AIDP console when available
- sign-out and session-management actions

The browser tests in [`apps/frontend/tests/security.test.mjs`](../apps/frontend/tests/security.test.mjs) are security-oriented source checks that prevent accidental regressions such as storing secrets in browser storage or changing password input semantics.

## UI implementation details
- `App.tsx` is a large but cohesive file that handles routing, modal focus control, password generation, and all API calls.
- `createPortal` is used for confirmation dialogs so the modal overlays the entire app.
- The app uses `credentials: "include"` on API calls because the backend session is cookie-based.
- The admin table distinguishes active, pending, inactive, and managed users so operators can see which identities belong to the lab.
- The styling file contains most of the layout and interaction behavior, so changing markup often requires a paired CSS update.

## Accessibility and security notes
- Dialogs trap focus and restore previous focus on close.
- Buttons that act as controls are explicitly `type="button"` so they do not submit forms accidentally.
- Passwords are generated with `crypto.getRandomValues` rather than a non-cryptographic source.
- The app is designed not to persist secrets in `localStorage` or `sessionStorage`.
- The `__Host-` admin session cookie is assumed to be handled entirely by the backend; the UI only checks session state via API calls.

## When changing the frontend
- If you change user lifecycle or admin flows, update the browser security tests at the same time.
- If you change the shape of API responses, verify the backend tests and the frontend together.
- If you change layout or responsive behavior, inspect `styles.css` and the accessibility implications of any new controls.
- Keep the admin UX aligned with the backend’s distinction between active, pending, and unmanaged identities.
