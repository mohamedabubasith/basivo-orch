# The SPA, and the Caddy that fronts the whole application.
#
# One container serves the built React app, terminates TLS, and reverse-proxies
# the API — so the browser only ever sees one origin. That makes the session
# cookie first-party and removes CORS from the deployment entirely, which is
# also why no API origin has to be baked in at build time.

FROM node:22-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Empty on purpose: the client then uses relative URLs and talks to whatever
# origin served the page. Baking in a hostname would mean rebuilding the image
# to change domains.
ENV VITE_API_URL=""

# The console's origin, when the landing page has a hostname of its own. This
# one HAS to be baked in: a session cookie belongs to a single origin, so the
# app needs one home, and the bundle cannot work out which of the two hostnames
# serving it is that home. Empty for a single-hostname deployment, where every
# link stays relative.
ARG CONSOLE_ORIGIN=""
ENV VITE_CONSOLE_ORIGIN=$CONSOLE_ORIGIN
RUN npm run build


FROM caddy:2.8-alpine AS runtime

COPY --from=builder /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

EXPOSE 80 443
