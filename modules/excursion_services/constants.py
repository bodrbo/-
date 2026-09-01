"""Bootstrap products for databases that predate the services catalog."""


DEFAULT_SERVICES = (
    ("Малый тур", 1.0, "group"),
    ("Средний тур", 1.5, "group"),
    ("Большой тур", 2.5, "group"),
    ("Аренда на 3 часа", 3.0, "individual"),
    ("Индивидуальная аренда 1 час", 1.0, "individual"),
    ("Индивидуальная аренда на 1.5 часа", 1.5, "individual"),
    ("Индивидуальная аренда 2 часа", 2.0, "individual"),
    ("Индивидуальная аренда на 2.5 часа", 2.5, "individual"),
)

SERVICE_TYPES = {
    "group": "Групповая экскурсия",
    "individual": "Индивидуальная экскурсия",
}
