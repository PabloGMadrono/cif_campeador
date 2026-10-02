#!/bin/sh
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$MINIO_BUCKET"
mc anonymous set none "local/$MINIO_BUCKET"

cat > /tmp/invoice-media-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$MINIO_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::$MINIO_BUCKET/originals/*"]
    }
  ]
}
EOF

mc admin policy create local invoice-media /tmp/invoice-media-policy.json
mc admin user add local "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
mc admin policy attach local invoice-media --user "$MINIO_ACCESS_KEY"
