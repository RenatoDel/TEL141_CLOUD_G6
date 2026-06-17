-- ============================================================================
-- 002_roles_courses.sql
--
-- Extiende el esquema con:
--   - 4 roles en usuario: admin, profesor, coach, alumno
--   - Tabla curso (cursos académicos dictados por un profesor)
--   - Tabla curso_alumno (inscripción M:N de alumnos en cursos)
--   - Usuarios y curso de demostración
--
-- Idempotente: se puede correr múltiples veces sin romper datos existentes.
--
-- Aplicación:
--   sudo docker exec -i pucp_mariadb mariadb -u pucp -ppucp_pass pucp_cloud \
--     < sql/migrations/002_roles_courses.sql
-- ============================================================================

USE pucp_cloud;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Extender el ENUM de roles
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE usuario
  MODIFY COLUMN rol ENUM('admin','profesor','coach','alumno')
  NOT NULL DEFAULT 'alumno';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Tabla curso
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS curso (
  id INT AUTO_INCREMENT PRIMARY KEY,
  codigo VARCHAR(20) NOT NULL UNIQUE,
  nombre VARCHAR(150) NOT NULL,
  profesor_id INT NULL,
  periodo VARCHAR(20) NOT NULL DEFAULT '2026-1',
  activo TINYINT(1) NOT NULL DEFAULT 1,
  creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_curso_profesor
    FOREIGN KEY (profesor_id) REFERENCES usuario(id) ON DELETE SET NULL
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Inscripción de alumnos en cursos (M:N)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS curso_alumno (
  curso_id INT NOT NULL,
  alumno_id INT NOT NULL,
  inscrito_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (curso_id, alumno_id),
  CONSTRAINT fk_ca_curso
    FOREIGN KEY (curso_id) REFERENCES curso(id) ON DELETE CASCADE,
  CONSTRAINT fk_ca_alumno
    FOREIGN KEY (alumno_id) REFERENCES usuario(id) ON DELETE CASCADE
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Bootstrap del admin con bcrypt real
--    (password = "admin123", hash generado con bcrypt cost 12)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE usuario
SET password_hash = '$2b$12$G2GBGjYXzGLWbE2/XbQ88euYs.Eo4h7KeZjkxAo0hy.yArHtYSPXi'
WHERE username = 'admin'
  AND (password_hash IS NULL
       OR password_hash = ''
       OR password_hash = '$2b$12$placeholder');

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Usuarios de demostración (idempotente con INSERT IGNORE)
--    Todos comparten el patrón <username>123 como password.
-- ─────────────────────────────────────────────────────────────────────────────
INSERT IGNORE INTO usuario (username, password_hash, email, rol) VALUES
  ('profesor1',
   '$2b$12$.pM3jmH7BPidZPKH/pv3UeYISsjTiFPbCc3CHkkSqI4yzRRl20gn6',
   'profesor1@pucp.edu.pe', 'profesor'),
  ('coach1',
   '$2b$12$mS5hDqgAQ2OGOtDsCWg7ru1Vv62Qd1M4YN7/ygnke08ZHbJ45Ju.e',
   'coach1@pucp.edu.pe', 'coach'),
  ('alumno1',
   '$2b$12$7kGdkT4eQ/EDObpY5e5JTu2SBH9rEZlyTEnyTW60tdYDxNRqtElYG',
   'alumno1@pucp.edu.pe', 'alumno'),
  ('alumno2',
   '$2b$12$7kGdkT4eQ/EDObpY5e5JTu2SBH9rEZlyTEnyTW60tdYDxNRqtElYG',
   'alumno2@pucp.edu.pe', 'alumno');

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Curso de demostración + inscripciones
-- ─────────────────────────────────────────────────────────────────────────────
INSERT IGNORE INTO curso (codigo, nombre, profesor_id, periodo)
  SELECT 'TEL141', 'Ingeniería de Redes Cloud', id, '2026-1'
    FROM usuario WHERE username = 'profesor1';

INSERT IGNORE INTO curso_alumno (curso_id, alumno_id)
  SELECT c.id, u.id
    FROM curso c
    JOIN usuario u ON u.username IN ('alumno1', 'alumno2')
   WHERE c.codigo = 'TEL141';
