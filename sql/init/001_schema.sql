CREATE DATABASE IF NOT EXISTS pucp_cloud;
USE pucp_cloud;

CREATE TABLE IF NOT EXISTS usuario (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  email VARCHAR(100) NOT NULL UNIQUE,
  rol ENUM('admin','usuario') NOT NULL DEFAULT 'usuario',
  activo TINYINT(1) NOT NULL DEFAULT 1,
  creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS servidor_fisico (
  id INT AUTO_INCREMENT PRIMARY KEY,
  nombre VARCHAR(50) NOT NULL UNIQUE,
  ip_interna VARCHAR(64) NOT NULL UNIQUE,
  zona_disponibilidad VARCHAR(32) NOT NULL,
  vcpus_total INT NOT NULL DEFAULT 4,
  ram_total_mb INT NOT NULL DEFAULT 8192,
  storage_total_gb INT NOT NULL DEFAULT 100,
  vms_activas INT NOT NULL DEFAULT 0,
  vcpus_used INT NOT NULL DEFAULT 0,
  ram_used_mb INT NOT NULL DEFAULT 0,
  storage_used_gb INT NOT NULL DEFAULT 0,
  activo TINYINT(1) NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS imagen (
  id INT AUTO_INCREMENT PRIMARY KEY,
  nombre VARCHAR(100) NOT NULL UNIQUE,
  filename VARCHAR(255) NOT NULL,
  os_type VARCHAR(50) NOT NULL,
  formato VARCHAR(20) NOT NULL DEFAULT 'qcow2',
  size_gb DECIMAL(10,2) NOT NULL DEFAULT 0,
  activa TINYINT(1) NOT NULL DEFAULT 1
);

INSERT IGNORE INTO usuario (id, username, password_hash, email, rol) VALUES
  (1, 'admin', '$2b$12$placeholder', 'admin@pucp.edu.pe', 'admin');

INSERT IGNORE INTO servidor_fisico (id, nombre, ip_interna, zona_disponibilidad, vcpus_total, ram_total_mb, storage_total_gb) VALUES
  (1, 'server1', '10.0.10.1', 'az-a', 4, 8192, 100),
  (2, 'server2', '10.0.10.2', 'az-a', 4, 8192, 100),
  (3, 'server3', '10.0.10.3', 'az-b', 4, 8192, 100);

INSERT IGNORE INTO imagen (id, nombre, filename, os_type, formato, size_gb, activa) VALUES
  (1, 'cirros-base.img', 'cirros-base.img', 'cirros', 'qcow2', 0.10, 1);
