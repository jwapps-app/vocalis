# Pinned to the builder's own architecture. Without --platform, a multi-arch
# build runs this whole stage again under QEMU to produce the arm64 image —
# npm install and a Vite build, emulated, which took the web image from a
# couple of minutes to the better part of an hour and dominated every release.
#
# The output is JavaScript, CSS and static assets: identical whatever machine
# emits them. Only the nginx layer below is genuinely architecture-specific, so
# that is the only part worth building twice.
FROM --platform=$BUILDPLATFORM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` over `npm install`: reproducible from the lockfile, and it does not
# quietly rewrite it during an image build where the change would be discarded.
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
