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
RUN npm run build


FROM caddy:2.8-alpine AS runtime

COPY --from=builder /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

EXPOSE 80 443
