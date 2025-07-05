from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from flask_cors import CORS
from dogpile.cache import make_region    # Redis cache preko dogpile.cache
from kafka import KafkaProducer, KafkaConsumer  # Kafka producer i consumer
from flask_socketio import SocketIO      # WebSocket komunikacija (real-time)
import json
import datetime
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tajni_kljuc'
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://user:password@mysql:3306/taskdb'  # MySQL baza podataka
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app)  # Omogućuje CORS
db = SQLAlchemy(app)  # SQLAlchemy ORM
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # Preusmjerava na login ako korisnik nije prijavljen
socketio = SocketIO(app, cors_allowed_origins="*")  # SocketIO za real-time događaje

# Konfiguracija Redis cache-a preko dogpile.cache biblioteke
# Redis se koristi za keširanje podataka (zadataka) i ubrzanje dohvaćanja iz baze
region = make_region().configure(
    'dogpile.cache.redis',
    arguments={
        'host': 'redis',            # Redis server (u Dockeru ili mreži)
        'port': 6379,
        'db': 0,
        'redis_expiration_time': 60,  # Vrijeme keširanja u sekundama
        'distributed_lock': True,
        'thread_local_lock': False,
    }
)

# Funkcija za kreiranje Kafka producer objekta
# Koristi se za slanje događaja promjena zadataka u Kafka topic
def get_kafka_producer():
    return KafkaProducer(
        bootstrap_servers=['kafka:9092'],  # Kafka broker adresa
        value_serializer=lambda v: json.dumps(v).encode('utf-8')  # Serijalizacija poruke u JSON
    )

# Kafka consumer koji sluša poruke na topicu 'tasks_topic'
# Prima poruke koje su poslali produceri i omogućuje real-time emit preko SocketIO
consumer = KafkaConsumer(
    'tasks_topic',
    bootstrap_servers=['kafka:9092'],
    value_deserializer=lambda m: json.loads(m.decode('utf-8')),  # Deserijalizacija JSON poruka
    group_id='task-consumer-group',
    auto_offset_reset='earliest'
)

kafka_thread = None  # Promjenjiva za dretvu koja će slušati Kafka poruke

# Definicija modela baze za grupe
class Group(db.Model):
    __tablename__ = 'groups'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(512))
    users = db.relationship('User', backref='group')

# Definicija modela baze za korisnike
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True)
    password_hash = db.Column(db.String(512))
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Definicija modela baze za zadatke
class Task(db.Model):
    __tablename__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100))
    description = db.Column(db.String(200))
    completed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'))

# Flask-Login funkcija za učitavanje korisnika iz baze po ID-u
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Funkcija za dohvat zadataka za određenu grupu iz baze
# Ovdje se koristi Redis cache da se smanji broj upita prema bazi
@region.cache_on_arguments()
def load_tasks_from_db(group_id):
    tasks = Task.query.filter_by(group_id=group_id).all()
    return [
        {
            'id': task.id,
            'title': task.title,
            'description': task.description,
            'completed': task.completed,
            'created_at': task.created_at.strftime('%Y-%m-%d %H:%M:%S')
        } for task in tasks
    ]

# Ruta početne stranice, prikazuje zadatke za grupu u kojoj je korisnik
@app.route('/')
@login_required
def index():
    if not current_user.group_id:
        return redirect(url_for('group'))  # Ako korisnik nije u grupi, preusmjeri na odabir grupe
    group = Group.query.get(current_user.group_id)
    return render_template('index.html', username=current_user.username, group_name=group.name if group else '')

# Ruta za registraciju korisnika
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        user = User(username=data.get('username'))
        user.set_password(data.get('password'))
        db.session.add(user)
        db.session.commit()
        return jsonify({'message': 'Registracija uspješna'})
    return render_template('register.html')

# Ruta za login korisnika
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        user = User.query.filter_by(username=data.get('username')).first()
        if user and user.check_password(data.get('password')):
            login_user(user)
            return jsonify({'message': 'Prijava uspješna'})
        return jsonify({'message': 'Neispravno korisničko ime ili lozinka'}), 401
    return render_template('login.html')

# Ruta za logout korisnika
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# Ruta za odabir/grupu kreiranje i pridruživanje
@app.route('/group', methods=['GET', 'POST'])
@login_required
def group():

    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')

        if action == 'create':
            if Group.query.filter_by(name=data.get('name')).first():
                return jsonify({'message': 'Grupa već postoji'}), 400
            group = Group(name=data.get('name'), password=generate_password_hash(data.get('password')))
            db.session.add(group)
            db.session.commit()
            current_user.group_id = group.id
            db.session.commit()

        elif action == 'join':
            group = Group.query.filter_by(name=data.get('name')).first()
            if not group or not check_password_hash(group.password, data.get('password')):
                return jsonify({'message': 'Neispravna grupa ili lozinka'}), 400
            current_user.group_id = group.id
            db.session.commit()

        return jsonify({'message': 'Grupa postavljena'})

    group = None
    if current_user.group_id:
        group = Group.query.get(current_user.group_id)

    return render_template('group.html', username=current_user.username, current_group=group)

# Ruta za dohvat svih zadataka korisnikove grupe (GET)
@app.route('/tasks', methods=['GET'])
@login_required
def get_tasks():
    tasks = load_tasks_from_db(current_user.group_id)  # Koristi Redis cache
    return jsonify(tasks)

# Ruta za kreiranje novog zadatka (POST)
@app.route('/tasks', methods=['POST'])
@login_required
def create_task():
    data = request.get_json()
    task = Task(
        title=data.get('title'),
        description=data.get('description'),
        completed=False,
        group_id=current_user.group_id
    )
    db.session.add(task)
    db.session.commit()
    send_task_event('create', task)  # Salje događaj u Kafka topic
    load_tasks_from_db.invalidate(current_user.group_id)  # Invalida cache za tu grupu
    return jsonify({'message': 'Zadatak kreiran'})

# Ruta za update zadatka (PUT)
@app.route('/tasks/<int:id>', methods=['PUT'])
@login_required
def update_task(id):
    task = db.session.get(Task, id)
    if not task or task.group_id != current_user.group_id:
        return jsonify({'message': 'Zadatak nije pronađen'}), 404

    data = request.get_json()
    task.title = data.get('title', task.title)
    task.description = data.get('description', task.description)
    task.completed = data.get('completed', task.completed)

    db.session.commit()
    send_task_event('update', task)  # Salje update event u Kafka
    load_tasks_from_db.invalidate(current_user.group_id)  # Invalida cache
    return jsonify({'message': 'Zadatak ažuriran'})

# Ruta za brisanje zadatka (DELETE)
@app.route('/tasks/<int:id>', methods=['DELETE'])
@login_required
def delete_task(id):
    task = db.session.get(Task, id)
    if not task or task.group_id != current_user.group_id:
        return jsonify({'message': 'Zadatak nije pronađen'}), 404

    send_task_event('delete', task)  # Salje delete event u Kafka
    db.session.delete(task)
    db.session.commit()
    load_tasks_from_db.invalidate(current_user.group_id)  # Invalida cache
    return jsonify({'message': 'Zadatak obrisan'})

# Funkcija koja šalje događaj promjene u Kafka topic 'tasks_topic'
def send_task_event(action, task):
    event = {
        'action': action,
        'task': {
            'id': task.id,
            'title': task.title,
            'description': task.description,
            'completed': task.completed,
            'group_id': task.group_id
        }
    }
    producer = get_kafka_producer()  # Kreira producer
    producer.send('tasks_topic', event)  # Šalje event u Kafka
    producer.flush()

# Funkcija koja u posebnoj dretvi sluša Kafka topic i emitira poruke preko SocketIO
def kafka_consumer_thread():
    for message in consumer:
        data = message.value
        print("Kafka poruka:", data)
        socketio.emit('task_event', data, namespace='/tasks')  # Emitira event real-time korisnicima

# SocketIO handler za novi spojeni klijent
@socketio.on('connect', namespace='/tasks')
def on_connect():
    global kafka_thread
    # Ako dretva koja prima Kafka poruke nije pokrenuta, pokreni je
    if kafka_thread is None or not kafka_thread.is_alive():
        kafka_thread = threading.Thread(target=kafka_consumer_thread)
        kafka_thread.daemon = True
        kafka_thread.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Kreiraj tablice ako ne postoje
    # Pokreni aplikaciju s podrškom za SocketIO
    socketio.run(app, debug=True, host='0.0.0.0', allow_unsafe_werkzeug=True)
