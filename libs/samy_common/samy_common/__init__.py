"""samy_common: nucleo compartido por todos los servicios de SAMY Cloud.

Contiene lo que DEBE ser identico en los cuatro despliegues: manejo de dinero,
maquina de estados, firma S2S, idempotencia, outbox de eventos y observabilidad.

Se distribuye como paquete instalable (no como codigo copiado) precisamente
porque una divergencia entre servicios en, por ejemplo, el redondeo de una
comision produciria descuadres contables imposibles de rastrear.
"""

__version__ = "0.1.0"
