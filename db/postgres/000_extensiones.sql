-- 000_extensiones.sql
-- btree_gist: permite combinar igualdad (=) y solapamiento (&&) en restricciones
-- de exclusión temporal (contenedores, sensores, membresías de conversación).
CREATE EXTENSION IF NOT EXISTS btree_gist;
