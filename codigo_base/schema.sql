-- MySQL dump 10.13  Distrib 8.0.45, for Win64 (x86_64)
--
-- Host: 127.0.0.1    Database: pucp_cloud
-- ------------------------------------------------------
-- Server version	8.0.33

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `enlace`
--

DROP TABLE IF EXISTS `enlace`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `enlace` (
  `id` int NOT NULL AUTO_INCREMENT,
  `slice_id` int NOT NULL,
  `vm_src` int NOT NULL,
  `vm_dst` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `slice_id` (`slice_id`),
  KEY `vm_src` (`vm_src`),
  KEY `vm_dst` (`vm_dst`),
  CONSTRAINT `enlace_ibfk_1` FOREIGN KEY (`slice_id`) REFERENCES `slice` (`id`) ON DELETE CASCADE,
  CONSTRAINT `enlace_ibfk_2` FOREIGN KEY (`vm_src`) REFERENCES `vm` (`id`) ON DELETE CASCADE,
  CONSTRAINT `enlace_ibfk_3` FOREIGN KEY (`vm_dst`) REFERENCES `vm` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `enlace`
--

LOCK TABLES `enlace` WRITE;
/*!40000 ALTER TABLE `enlace` DISABLE KEYS */;
/*!40000 ALTER TABLE `enlace` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `job`
--

DROP TABLE IF EXISTS `job`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `job` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_uid` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `slice_id` int NOT NULL,
  `tipo` enum('create','delete') COLLATE utf8mb4_unicode_ci NOT NULL,
  `estado` enum('queued','running','completed','failed') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'queued',
  `progreso` json DEFAULT NULL,
  `error` text COLLATE utf8mb4_unicode_ci,
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `actualizado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `job_uid` (`job_uid`),
  KEY `slice_id` (`slice_id`),
  CONSTRAINT `job_ibfk_1` FOREIGN KEY (`slice_id`) REFERENCES `slice` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `job`
--

LOCK TABLES `job` WRITE;
/*!40000 ALTER TABLE `job` DISABLE KEYS */;
/*!40000 ALTER TABLE `job` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `servidor_fisico`
--

DROP TABLE IF EXISTS `servidor_fisico`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `servidor_fisico` (
  `id` int NOT NULL AUTO_INCREMENT,
  `nombre` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `ip_interna` varchar(15) COLLATE utf8mb4_unicode_ci NOT NULL,
  `vcpus_total` int NOT NULL DEFAULT '4',
  `ram_total_mb` int NOT NULL DEFAULT '8192',
  `activo` tinyint(1) NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `nombre` (`nombre`),
  UNIQUE KEY `ip_interna` (`ip_interna`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `servidor_fisico`
--

LOCK TABLES `servidor_fisico` WRITE;
/*!40000 ALTER TABLE `servidor_fisico` DISABLE KEYS */;
INSERT INTO `servidor_fisico` VALUES (1,'server1','10.0.10.1',4,8192,1),(2,'server2','10.0.10.2',4,8192,1),(3,'server3','10.0.10.3',4,8192,1);
/*!40000 ALTER TABLE `servidor_fisico` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `slice`
--

DROP TABLE IF EXISTS `slice`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `slice` (
  `id` int NOT NULL AUTO_INCREMENT,
  `slice_uid` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `nombre` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `usuario_id` int NOT NULL,
  `topologia_id` int NOT NULL,
  `vlan_id` int NOT NULL,
  `cidr` varchar(18) COLLATE utf8mb4_unicode_ci NOT NULL,
  `estado` enum('creating','running','deleting','error','deleted') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'creating',
  `tiene_internet` tinyint(1) NOT NULL DEFAULT '0',
  `tiene_dhcp` tinyint(1) NOT NULL DEFAULT '0',
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `actualizado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `slice_uid` (`slice_uid`),
  UNIQUE KEY `vlan_id` (`vlan_id`),
  KEY `usuario_id` (`usuario_id`),
  KEY `topologia_id` (`topologia_id`),
  CONSTRAINT `slice_ibfk_1` FOREIGN KEY (`usuario_id`) REFERENCES `usuario` (`id`),
  CONSTRAINT `slice_ibfk_2` FOREIGN KEY (`topologia_id`) REFERENCES `topologia` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `slice`
--

LOCK TABLES `slice` WRITE;
/*!40000 ALTER TABLE `slice` DISABLE KEYS */;
/*!40000 ALTER TABLE `slice` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `token_jwt`
--

DROP TABLE IF EXISTS `token_jwt`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `token_jwt` (
  `id` int NOT NULL AUTO_INCREMENT,
  `usuario_id` int NOT NULL,
  `token` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `expira_en` datetime NOT NULL,
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `usuario_id` (`usuario_id`),
  CONSTRAINT `token_jwt_ibfk_1` FOREIGN KEY (`usuario_id`) REFERENCES `usuario` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `token_jwt`
--

LOCK TABLES `token_jwt` WRITE;
/*!40000 ALTER TABLE `token_jwt` DISABLE KEYS */;
/*!40000 ALTER TABLE `token_jwt` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `topologia`
--

DROP TABLE IF EXISTS `topologia`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `topologia` (
  `id` int NOT NULL AUTO_INCREMENT,
  `nombre` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `descripcion` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `nombre` (`nombre`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `topologia`
--

LOCK TABLES `topologia` WRITE;
/*!40000 ALTER TABLE `topologia` DISABLE KEYS */;
INSERT INTO `topologia` VALUES (1,'linear','VMs conectadas en cadena — VM1─VM2─VM3'),(2,'ring','VMs conectadas en anillo con enlace de cierre — mínimo 3 VMs');
/*!40000 ALTER TABLE `topologia` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `usuario`
--

DROP TABLE IF EXISTS `usuario`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `usuario` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `password_hash` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `email` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `rol` enum('admin','usuario') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'usuario',
  `activo` tinyint(1) NOT NULL DEFAULT '1',
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`),
  UNIQUE KEY `email` (`email`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `usuario`
--

LOCK TABLES `usuario` WRITE;
/*!40000 ALTER TABLE `usuario` DISABLE KEYS */;
INSERT INTO `usuario` VALUES (1,'admin','$2b$12$placeholder','admin@pucp.edu.pe','admin',1,'2026-05-11 02:23:07');
/*!40000 ALTER TABLE `usuario` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `vm`
--

DROP TABLE IF EXISTS `vm`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `vm` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vm_uid` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `nombre` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `slice_id` int NOT NULL,
  `servidor_id` int NOT NULL,
  `vnc_port` int NOT NULL,
  `ram_mb` int NOT NULL DEFAULT '256',
  `vcpus` int NOT NULL DEFAULT '1',
  `estado` enum('creating','running','stopped','error','deleted') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'creating',
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `vm_uid` (`vm_uid`),
  KEY `slice_id` (`slice_id`),
  KEY `servidor_id` (`servidor_id`),
  CONSTRAINT `vm_ibfk_1` FOREIGN KEY (`slice_id`) REFERENCES `slice` (`id`) ON DELETE CASCADE,
  CONSTRAINT `vm_ibfk_2` FOREIGN KEY (`servidor_id`) REFERENCES `servidor_fisico` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `vm`
--

LOCK TABLES `vm` WRITE;
/*!40000 ALTER TABLE `vm` DISABLE KEYS */;
/*!40000 ALTER TABLE `vm` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-05-11  2:31:13
