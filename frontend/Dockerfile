FROM node:20-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY 40-htpasswd.sh /docker-entrypoint.d/40-htpasswd.sh
RUN chmod +x /docker-entrypoint.d/40-htpasswd.sh
COPY --from=build /src/dist /usr/share/nginx/html
