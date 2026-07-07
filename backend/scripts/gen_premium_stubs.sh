#!/usr/bin/env sh
# Gera os stubs Python do contrato premium.v1 para o GrpcTransport do core.
# Requer: pip install grpcio-tools  (só build-time; a imagem base gera os stubs).
# Correr a partir de docker/backend/ :  sh scripts/gen_premium_stubs.sh
set -e

PROTO_DIR="app/premium/proto"

python -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$PROTO_DIR" \
    --grpc_python_out="$PROTO_DIR" \
    "$PROTO_DIR/premium.proto"

# O grpc_tools gera `import premium_pb2` (absoluto), que falha quando importado
# como app.premium.proto.premium_pb2_grpc. Tornar o import relativo ao pacote.
if [ -f "$PROTO_DIR/premium_pb2_grpc.py" ]; then
    sed -i 's/^import premium_pb2/from . import premium_pb2/' "$PROTO_DIR/premium_pb2_grpc.py"
fi

echo "Stubs gerados em $PROTO_DIR (premium_pb2.py, premium_pb2_grpc.py)"
