#!/usr/bin/env python3
"""
부하테스트 중 시스템 상태를 모니터링하는 대시보드
DB, Celery, Valkey, Docker 리소스 등 실시간 확인

사용법:
    python3 load_test_monitor.py
"""

import subprocess
import json
import time
import sys
import os
from datetime import datetime
from collections import deque
from typing import Dict, Any, Optional
import signal

try:
    import psutil
except ImportError:
    print("psutil 설치 필요: pip install psutil")
    sys.exit(1)

class SystemMonitor:
    """시스템 상태 모니터링"""

    def __init__(self, db_host: str = "localhost", db_user: str = "root", db_pass: str = ""):
        self.db_host = db_host
        self.db_user = db_user
        self.db_pass = db_pass
        self.db_name = "AgnMonitor"

        self.history = deque(maxlen=60)  # 최근 60개 샘플 저장
        self.running = True
        self.start_time = datetime.now()

    def get_docker_stats(self) -> Dict[str, Any]:
        """Docker 컨테이너 리소스 사용량"""
        try:
            # docker stats 명령으로 간단하게 조회
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
                capture_output=True,
                text=True,
                timeout=5
            )

            stats = {}
            lines = result.stdout.strip().split('\n')

            # 첫 번째 줄은 헤더이므로 건너뛰기
            for line in lines[1:]:
                if not line.strip():
                    continue

                parts = line.split()
                if len(parts) >= 3:
                    container_id = parts[0][:12]  # 짧은 ID
                    cpu = parts[1] if len(parts) > 1 else '0%'
                    mem = ' '.join(parts[2:]) if len(parts) > 2 else '0MB / 0MB'

                    # 컨테이너 이름으로 변환
                    name_result = subprocess.run(
                        ["docker", "inspect", "-f", "{{.Name}}", container_id],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    container_name = name_result.stdout.strip().strip('/')

                    stats[container_name] = {
                        'cpu': cpu,
                        'memory': mem,
                    }

            return stats if stats else {'info': 'No containers running'}
        except Exception as e:
            return {'error': str(e)}

    def get_db_stats(self) -> Dict[str, Any]:
        """MariaDB 통계"""
        try:
            queries = [
                ("connections", "SHOW STATUS LIKE 'Threads_connected';"),
                ("slow_queries", "SHOW STATUS LIKE 'Slow_queries';"),
                ("questions", "SHOW STATUS LIKE 'Questions';"),
                ("metric_count", f"SELECT COUNT(*) FROM {self.db_name}.core_dashboard_monitoringmetric;"),
                ("server_count", f"SELECT COUNT(*) FROM {self.db_name}.core_dashboard_managedserver;"),
            ]

            stats = {}
            for stat_name, query in queries:
                try:
                    # Docker exec를 통해 mysql 명령 실행
                    result = subprocess.run(
                        ["docker", "exec", "mariadb_AgnMonitor", "mysql",
                         "-u", self.db_user, f"-p{self.db_pass}",
                         "-se", query],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    if result.returncode == 0:
                        output = result.stdout.strip()
                        if stat_name == 'metric_count' or stat_name == 'server_count':
                            stats[stat_name] = int(output) if output else 0
                        else:
                            # "Threads_connected\t5" 형식
                            parts = output.split('\t')
                            stats[stat_name] = int(parts[-1]) if parts and parts[-1].isdigit() else 0
                    else:
                        stats[stat_name] = "N/A"
                except Exception as e:
                    stats[stat_name] = "N/A"

            return stats if stats else {'info': 'Database unavailable'}
        except Exception as e:
            return {'error': str(e)}

    def get_cache_stats(self) -> Dict[str, Any]:
        """Redis/Valkey 통계"""
        try:
            result = subprocess.run(
                ["docker", "exec", "valkey_AgnMonitor", "valkey-cli", "INFO", "memory"],
                capture_output=True,
                text=True,
                timeout=5
            )

            stats = {}
            for line in result.stdout.strip().split('\n'):
                if ':' in line and not line.startswith('#'):
                    key, value = line.split(':', 1)
                    if 'used_memory' in key or 'peak_memory' in key:
                        try:
                            # 숫자로 변환 시도
                            mem_bytes = int(value.split()[0]) if value else 0
                            stats[key] = f"{mem_bytes / (1024*1024):.2f}MB"
                        except (ValueError, IndexError):
                            stats[key] = value
                    elif 'evicted_keys' in key:
                        stats[key] = value

            return stats if stats else {'info': 'No memory stats'}
        except Exception as e:
            return {'error': str(e)}

    def get_django_logs_tail(self, lines: int = 10) -> list:
        """Django 로그 최근 항목"""
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", "--tail", str(lines), "monitor"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd="/opt/AgnMonitor"
            )

            return result.stdout.strip().split('\n')[-lines:]
        except Exception as e:
            return [f"Error: {str(e)}"]

    def get_system_load(self) -> Dict[str, Any]:
        """시스템 로드"""
        try:
            load_avg = os.getloadavg()
            return {
                'load_1min': f"{load_avg[0]:.2f}",
                'load_5min': f"{load_avg[1]:.2f}",
                'load_15min': f"{load_avg[2]:.2f}",
            }
        except:
            return {}

    def get_disk_usage(self) -> Dict[str, Any]:
        """디스크 사용량"""
        try:
            usage = psutil.disk_usage('/')
            return {
                'total': f"{usage.total / (1024**3):.2f}GB",
                'used': f"{usage.used / (1024**3):.2f}GB",
                'percent': f"{usage.percent:.1f}%"
            }
        except:
            return {}

    def print_screen(self):
        """화면 업데이트"""
        os.system('clear' if os.name != 'nt' else 'cls')

        elapsed = (datetime.now() - self.start_time).total_seconds()
        print(f"\n{'='*80}")
        print(f"AgnMonitor LOAD TEST MONITORING DASHBOARD")
        print(f"{'='*80}")
        print(f"Elapsed Time: {int(elapsed)}s | Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}\n")

        # Docker Stats
        print("[DOCKER STATS]")
        docker_stats = self.get_docker_stats()
        if 'error' in docker_stats:
            print(f"  Error: {docker_stats['error']}")
        elif 'info' in docker_stats:
            print(f"  {docker_stats['info']}")
        else:
            for container, stats in docker_stats.items():
                if isinstance(stats, dict):
                    print(f"  {container}:")
                    print(f"    CPU: {stats.get('cpu', 'N/A')}")
                    print(f"    Memory: {stats.get('memory', 'N/A')}")

        # System Load
        print("\n[SYSTEM LOAD]")
        system_load = self.get_system_load()
        print(f"  Load Average: {system_load.get('load_1min', 'N/A')} / {system_load.get('load_5min', 'N/A')} / {system_load.get('load_15min', 'N/A')}")

        # Disk Usage
        print("\n[DISK USAGE]")
        disk_usage = self.get_disk_usage()
        print(f"  Total: {disk_usage.get('total', 'N/A')}")
        print(f"  Used: {disk_usage.get('used', 'N/A')} ({disk_usage.get('percent', 'N/A')})")

        # Database Stats
        print("\n[DATABASE (MariaDB)]")
        db_stats = self.get_db_stats()
        if 'error' not in db_stats:
            print(f"  Connected Threads: {db_stats.get('connections', 'N/A')}")
            print(f"  Slow Queries: {db_stats.get('slow_queries', 'N/A')}")
            print(f"  Total Queries: {db_stats.get('questions', 'N/A')}")
            print(f"  Monitoring Metrics: {db_stats.get('metric_count', 'N/A'):,}")
            print(f"  Registered Servers: {db_stats.get('server_count', 'N/A')}")
        else:
            print(f"  Error: {db_stats['error']}")

        # Cache Stats
        print("\n[CACHE (Valkey/Redis)]")
        cache_stats = self.get_cache_stats()
        if 'error' not in cache_stats:
            for key, value in cache_stats.items():
                print(f"  {key}: {value}")
        else:
            print(f"  Error: {cache_stats['error']}")

        # Recent Logs
        print("\n[RECENT DJANGO LOGS]")
        logs = self.get_django_logs_tail(lines=5)
        for log in logs:
            if log.strip():
                # 로그 길이 제한
                log_text = log[:100] + '...' if len(log) > 100 else log
                print(f"  {log_text}")

        print(f"\n{'='*80}")
        print("Press Ctrl+C to exit | Updates every 5 seconds")
        print(f"{'='*80}\n")

    def run(self, interval: int = 5):
        """주기적으로 모니터링 실행"""
        def signal_handler(sig, frame):
            print("\n\nMonitoring stopped.")
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)

        while self.running:
            try:
                self.print_screen()
                time.sleep(interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(interval)

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Load Test Monitoring Dashboard for AgnMonitor'
    )
    parser.add_argument('--interval', type=int, default=5,
                        help='Update interval in seconds (default: 5)')
    parser.add_argument('--db-host', type=str, default='localhost',
                        help='MariaDB host (default: localhost)')
    parser.add_argument('--db-user', type=str, default='root',
                        help='MariaDB user (default: root)')
    parser.add_argument('--db-pass', type=str, default=os.getenv('DB_PASSWORD', ''),
                        help='MariaDB password (default: $DB_PASSWORD)')

    args = parser.parse_args()

    monitor = SystemMonitor(
        db_host=args.db_host,
        db_user=args.db_user,
        db_pass=args.db_pass
    )

    monitor.run(interval=args.interval)

if __name__ == '__main__':
    main()
