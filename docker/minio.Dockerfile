# Build the pinned official source release; newer community releases are source-only.
FROM golang:1.24.9-alpine AS server-build
RUN CGO_ENABLED=0 go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM golang:1.24.9-alpine AS client-build
RUN CGO_ENABLED=0 go install github.com/minio/mc@RELEASE.2025-08-13T08-35-41Z

FROM alpine:3.22.2 AS server
RUN apk add --no-cache ca-certificates && mkdir /data && chown 1000:1000 /data
COPY --from=server-build /go/bin/minio /usr/local/bin/minio
ENV HOME=/tmp
USER 1000:1000
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]

FROM alpine:3.22.2 AS provisioner
RUN apk add --no-cache ca-certificates
COPY --from=client-build /go/bin/mc /usr/local/bin/mc
COPY provision-minio.sh /usr/local/bin/provision-minio.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/provision-minio.sh"]
