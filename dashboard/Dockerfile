FROM node:20-alpine AS base

WORKDIR /app

# Install dependencies
COPY dashboard/package.json ./
RUN npm install

# Copy application source
COPY dashboard ./

# Build Next.js application
RUN npm run build

EXPOSE 3000

ENV PORT=3000
ENV NODE_ENV=production

CMD ["npm", "start"]
