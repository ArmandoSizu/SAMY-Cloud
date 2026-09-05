# Backlog de SAMY Cloud

Funcionalidades **fuera del alcance actual**. Se registran aquí para no perderlas y,
sobre todo, para no construirlas antes de tiempo.

Prioridad inmediata: que los tres microservicios evaluables funcionen de verdad.

---

## Fuera de alcance por decisión explícita

Pedido expresamente dejar fuera hasta que lo esencial funcione:

- Tienda en línea / catálogo de productos propios
- Pedido de despensa
- Delivery y logística
- Inventario avanzado
- Inteligencia artificial (predicción de demanda, detección de fraude por ML)

---

## Siguiente (una vez cerrado el alcance actual)

### Producto
- [ ] Envío digital del comprobante por WhatsApp o correo
- [ ] Corte de caja por turno con arqueo de efectivo
- [ ] Reporte mensual descargable (XLSX) para contabilidad
- [ ] Planes de suscripción con distinto reparto de comisión
- [ ] Alta de tiendas autoservicio (onboarding sin intervención nuestra)

### Microservicios adicionales
- [ ] Pago de más servicios: gas, internet, TV, predial
- [ ] Venta de pines de contenido digital
- [ ] Retiro de efectivo / corresponsalía bancaria *(requiere licencia)*

### Técnico
- [ ] CI/CD con pruebas y análisis estático en cada PR
- [ ] Pruebas end-to-end con Playwright sobre el flujo vertical
- [ ] Observabilidad externa (OpenTelemetry, trazas distribuidas)
- [ ] Modo offline parcial en la PWA para consultar historial sin señal
- [ ] Rotación automática del secreto S2S
- [ ] Réplica de lectura para reportes pesados

### Seguridad
- [ ] Segundo factor para el rol STORE_OWNER
- [ ] Detección de anomalías (recargas atípicas, montos fuera de patrón)
- [ ] Exportación de auditoría en formato inmutable para revisión externa
- [ ] Pentest antes de operar con dinero real

---

## Ideas sin compromiso

- Aplicación nativa con la API REST existente
- Panel para que el proveedor de recargas vea su volumen
- Marketplace de servicios de terceros
- Cashback o programa de lealtad para el cliente final
