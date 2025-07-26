from app import app, db

with app.app_context():
    print("Creando todas las tablas en la base de datos...")
    db.create_all()
    print("¡Tablas creadas exitosamente!")