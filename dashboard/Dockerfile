# Build stage
FROM node:24-alpine AS build

WORKDIR /app

# Reproducible installs from the lock file
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY dashboard ./

# The dashboard proxies /api and /v1 to the API (next.config.js). Next.js fixes these
# rewrites at build time, so the API address must be known here, not only at runtime.
ARG API_URL=http://api:8000
ENV API_URL=$API_URL \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# Runtime stage: only the standalone server, running as a non-root user
FROM node:24-alpine

WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000

CMD ["node", "server.js"]
