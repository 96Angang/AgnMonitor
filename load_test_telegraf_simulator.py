#!/usr/bin/env python3
"""
40대 서버 규모의 부하테스트 시뮬레이터
3대 실제 서버 + 36대 모의 서버 = 총 40대
2초 간격으로 메트릭 송신 (Telegraf 형식)

사용법:
    python3 load_test_telegraf_simulator.py --servers 36 --interval 2 --duration 3600
"""

import json
import random
import time
import requests
import argparse
import threading
import sys
import signal
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TelegrafSimulator:
    """Telegraf 형식의 메트릭을 생성하고 전송하는 시뮬레이터"""

    def __init__(self,
                 api_url: str = "http://localhost:18080/api/collect/",
                 num_servers: int = 36,
                 num_metrics_per_server: int = 20,
                 interval: int = 2):
        self.api_url = api_url
        self.num_servers = num_servers
        self.num_metrics_per_server = num_metrics_per_server
        self.interval = interval

        # 실제 등록된 3대 + 모의 36대
        self.real_servers = ['server1', 'server2', 'server3']  # 실제 서버명 확인 후 수정
        self.mock_servers = [f'mock-server-{i:02d}' for i in range(1, num_servers + 1)]
        self.all_servers = self.real_servers + self.mock_servers

        # 메트릭 소스 및 필드 정의
        self.metric_sources = {
            'cpu': {
                'fields': {'usage_idle': 'float', 'usage_user': 'float', 'usage_system': 'float'},
                'tags': {'cpu': 'cpu-total'}
            },
            'mem': {
                'fields': {'used_percent': 'float', 'available_percent': 'float'},
                'tags': {}
            },
            'disk': {
                'fields': {'used_percent': 'float', 'free': 'int'},
                'tags': {'path': '/'}
            },
            'net': {
                'fields': {'bytes_recv': 'int', 'bytes_sent': 'int', 'packets_recv': 'int'},
                'tags': {'interface': 'eth0'}
            },
            'diskio': {
                'fields': {'reads': 'int', 'writes': 'int', 'read_bytes': 'int'},
                'tags': {'name': 'sda'}
            },
            'system': {
                'fields': {'load1': 'float', 'load5': 'float', 'load15': 'float'},
                'tags': {}
            },
            'processes': {
                'fields': {'running': 'int', 'sleeping': 'int', 'total': 'int'},
                'tags': {}
            },
            'custom_logs': {
                'fields': {'value': 'str'},
                'tags': {'source': 'application'}
            },
        }

        # 통계 정보
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_metrics_sent': 0,
            'errors': defaultdict(int),
            'response_times': [],
            'start_time': None,
            'end_time': None,
        }

        self.running = True
        self.stop_event = threading.Event()

    def generate_metric(self, hostname: str, source: str) -> Dict[str, Any]:
        """Telegraf 형식의 메트릭 생성"""

        metric_config = self.metric_sources[source]
        fields = {}

        # 필드값 생성 (realistic 범위)
        for field_name, field_type in metric_config['fields'].items():
            if field_type == 'float':
                # CPU, memory 등 percentage
                if 'percent' in field_name or 'idle' in field_name:
                    fields[field_name] = round(random.uniform(10, 90), 2)
                # Load average
                elif 'load' in field_name:
                    fields[field_name] = round(random.uniform(0.5, 4.0), 2)
                else:
                    fields[field_name] = round(random.uniform(0, 100), 2)

            elif field_type == 'int':
                # bytes, packets 등 카운터
                if 'bytes' in field_name or 'recv' in field_name or 'sent' in field_name:
                    fields[field_name] = random.randint(1000000, 100000000)
                elif 'packets' in field_name:
                    fields[field_name] = random.randint(10000, 1000000)
                else:
                    fields[field_name] = random.randint(1, 1000)

            elif field_type == 'str':
                # 로그 메시지
                log_messages = [
                    'INFO: Request processed successfully',
                    'WARNING: High memory usage detected',
                    'ERROR: Connection timeout',
                    'DEBUG: Cache hit',
                ]
                fields[field_name] = random.choice(log_messages)

        # 태그 생성
        tags = {
            'host': hostname,
            **metric_config['tags']
        }

        return {
            'name': source,
            'tags': tags,
            'fields': fields,
            'timestamp': int(time.time() * 1e9)  # nanoseconds
        }

    def generate_batch(self) -> List[Dict[str, Any]]:
        """모든 서버의 메트릭 배치 생성"""
        metrics = []

        for hostname in self.all_servers:
            # 각 서버당 무작위 개수의 메트릭 (15-20개)
            num_metrics = random.randint(15, self.num_metrics_per_server)

            # 매번 같은 소스를 선택하지 않도록 (realistic)
            selected_sources = random.sample(
                list(self.metric_sources.keys()),
                min(num_metrics, len(self.metric_sources))
            )

            for source in selected_sources:
                metric = self.generate_metric(hostname, source)
                metrics.append(metric)

        self.stats['total_metrics_sent'] += len(metrics)
        return metrics

    def send_metrics(self, metrics: List[Dict[str, Any]]) -> bool:
        """메트릭 배치 전송"""
        try:
            start_time = time.time()

            response = requests.post(
                self.api_url,
                json=metrics,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )

            elapsed = time.time() - start_time
            self.stats['response_times'].append(elapsed)
            self.stats['total_requests'] += 1

            if response.status_code == 200:
                self.stats['successful_requests'] += 1
                return True
            else:
                self.stats['failed_requests'] += 1
                self.stats['errors'][f'HTTP_{response.status_code}'] += 1
                logger.warning(f"HTTP {response.status_code}: {response.text[:100]}")
                return False

        except requests.exceptions.Timeout:
            self.stats['failed_requests'] += 1
            self.stats['errors']['Timeout'] += 1
            logger.error("Request timeout")
            return False

        except requests.exceptions.ConnectionError:
            self.stats['failed_requests'] += 1
            self.stats['errors']['ConnectionError'] += 1
            logger.error("Connection error")
            return False

        except Exception as e:
            self.stats['failed_requests'] += 1
            self.stats['errors'][type(e).__name__] += 1
            logger.error(f"Error: {str(e)}")
            return False

    def run_simulation(self, duration: int = 3600):
        """부하테스트 실행

        Args:
            duration: 테스트 지속 시간 (초). 0이면 무한 실행
        """
        self.stats['start_time'] = datetime.now()
        logger.info(f"Starting load test simulation")
        logger.info(f"  - Servers: {len(self.all_servers)} ({len(self.real_servers)} real + {self.num_servers} mock)")
        logger.info(f"  - Metrics per batch: ~{len(self.all_servers) * self.num_metrics_per_server}")
        logger.info(f"  - Interval: {self.interval}s")
        logger.info(f"  - Duration: {'infinite' if duration == 0 else f'{duration}s'}")
        logger.info(f"  - API URL: {self.api_url}")
        logger.info("")

        iteration = 0
        start_time = time.time()

        try:
            while self.running:
                iteration += 1
                batch_start = time.time()

                # 메트릭 배치 생성
                metrics = self.generate_batch()

                # 메트릭 전송
                success = self.send_metrics(metrics)

                batch_elapsed = time.time() - batch_start

                # 통계 출력 (10회마다)
                if iteration % 10 == 0:
                    self.print_stats(iteration)

                # 다음 배치까지 대기 (interval 유지)
                sleep_time = max(0, self.interval - batch_elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

                # 지속시간 체크
                if duration > 0 and (time.time() - start_time) >= duration:
                    logger.info(f"\nDuration reached ({duration}s). Stopping...")
                    break

        except KeyboardInterrupt:
            logger.info("\n\nStopped by user (Ctrl+C)")

        finally:
            self.running = False
            self.stats['end_time'] = datetime.now()
            self.print_final_stats()

    def print_stats(self, iteration: int):
        """현재 통계 출력"""
        elapsed = (datetime.now() - self.stats['start_time']).total_seconds()

        success_rate = (
            self.stats['successful_requests'] / self.stats['total_requests'] * 100
            if self.stats['total_requests'] > 0 else 0
        )

        avg_response_time = (
            sum(self.stats['response_times']) / len(self.stats['response_times'])
            if self.stats['response_times'] else 0
        )

        print(f"\n[Iteration {iteration}] (Elapsed: {elapsed:.0f}s)")
        print(f"  Requests: {self.stats['total_requests']:,} "
              f"(Success: {self.stats['successful_requests']:,} / "
              f"Failed: {self.stats['failed_requests']:,}) "
              f"- Success Rate: {success_rate:.1f}%")
        print(f"  Metrics Sent: {self.stats['total_metrics_sent']:,}")
        print(f"  Avg Response Time: {avg_response_time*1000:.2f}ms")

        if self.stats['errors']:
            print(f"  Errors: {dict(self.stats['errors'])}")

    def print_final_stats(self):
        """최종 통계 출력"""
        if not self.stats['start_time'] or not self.stats['end_time']:
            return

        total_duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        success_rate = (
            self.stats['successful_requests'] / self.stats['total_requests'] * 100
            if self.stats['total_requests'] > 0 else 0
        )

        avg_response_time = (
            sum(self.stats['response_times']) / len(self.stats['response_times'])
            if self.stats['response_times'] else 0
        )

        min_response_time = min(self.stats['response_times']) if self.stats['response_times'] else 0
        max_response_time = max(self.stats['response_times']) if self.stats['response_times'] else 0

        requests_per_sec = self.stats['total_requests'] / total_duration if total_duration > 0 else 0
        metrics_per_sec = self.stats['total_metrics_sent'] / total_duration if total_duration > 0 else 0

        print("\n" + "="*70)
        print("FINAL STATISTICS")
        print("="*70)
        print(f"Test Duration: {total_duration:.2f}s")
        print(f"\nRequest Statistics:")
        print(f"  Total Requests: {self.stats['total_requests']:,}")
        print(f"  Successful: {self.stats['successful_requests']:,} ({success_rate:.1f}%)")
        print(f"  Failed: {self.stats['failed_requests']:,}")
        print(f"  Requests/sec: {requests_per_sec:.2f}")

        print(f"\nMetrics Statistics:")
        print(f"  Total Metrics Sent: {self.stats['total_metrics_sent']:,}")
        print(f"  Metrics/sec: {metrics_per_sec:.2f}")

        print(f"\nResponse Time Statistics (ms):")
        print(f"  Average: {avg_response_time*1000:.2f}ms")
        print(f"  Min: {min_response_time*1000:.2f}ms")
        print(f"  Max: {max_response_time*1000:.2f}ms")

        if self.stats['errors']:
            print(f"\nErrors:")
            for error_type, count in sorted(self.stats['errors'].items(), key=lambda x: -x[1]):
                print(f"  {error_type}: {count}")

        print("="*70)

def main():
    parser = argparse.ArgumentParser(
        description='Telegraf Load Test Simulator for AgnMonitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # 36대 모의 서버, 2초 간격, 1시간 테스트
  python3 load_test_telegraf_simulator.py --servers 36 --interval 2 --duration 3600

  # 40대 (실제 3대 + 모의 37대), 2초 간격, 무한 실행
  python3 load_test_telegraf_simulator.py --servers 37 --interval 2 --duration 0

  # 기본 설정 (36대, 2초 간격, 1시간)
  python3 load_test_telegraf_simulator.py
        '''
    )

    parser.add_argument('--servers', type=int, default=36,
                        help='Number of mock servers to simulate (default: 36)')
    parser.add_argument('--url', type=str, default='http://localhost:18080/api/collect/',
                        help='API endpoint URL (default: http://localhost:18080/api/collect/)')
    parser.add_argument('--interval', type=int, default=2,
                        help='Metric send interval in seconds (default: 2)')
    parser.add_argument('--duration', type=int, default=3600,
                        help='Test duration in seconds, 0 for infinite (default: 3600)')
    parser.add_argument('--metrics-per-server', type=int, default=20,
                        help='Average metrics per server (default: 20)')

    args = parser.parse_args()

    # 시뮬레이터 생성 및 실행
    simulator = TelegrafSimulator(
        api_url=args.url,
        num_servers=args.servers,
        num_metrics_per_server=args.metrics_per_server,
        interval=args.interval
    )

    # Ctrl+C 처리
    def signal_handler(sig, frame):
        logger.info("\nReceived interrupt signal")
        simulator.running = False

    signal.signal(signal.SIGINT, signal_handler)

    # 테스트 실행
    simulator.run_simulation(duration=args.duration)

if __name__ == '__main__':
    main()
