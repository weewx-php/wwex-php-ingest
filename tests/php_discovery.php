<?php

declare(strict_types=1);

require $argv[1] . '/src/autoload.php';

use WeewxPhp\Admin\ReadModel;
use WeewxPhp\Admin\Service;
use WeewxPhp\Ingest\NativeReceiver;
use WeewxPhp\Log\MemoryLogger;
use WeewxPhp\Tick\Runtime;
use WeewxPhp\Time\FixedClock;

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

$directory = $argv[2];
$path = $directory . '/weather.conf';
file_put_contents($path, "data_dir = $directory\ntimezone = UTC\n[Ingest]\nenabled = true\nmax_pending = 2\n");
$now = 1788609600;
$runtime = Runtime::boot($path, new FixedClock($now), new MemoryLogger());
$receiver = new NativeReceiver($runtime);
$collector = '11111111-1111-4111-8111-111111111111';
$token = bin2hex(random_bytes(32));
$event = ['station_id' => '22222222-2222-4222-8222-222222222222',
    'event_id' => '33333333-3333-4333-8333-333333333333', 'kind' => 'loop',
    'driver_module' => 'weewx.drivers.simulator', 'dateTime' => $now - 1,
    'usUnits' => 17, 'data' => ['outTemp' => 18.2, 'rain' => 0.2]];
$send = static function (string $id, string $credential, array $packet) use ($receiver) {
    $body = json_encode(['version' => 1, 'collector_id' => $id, 'packets' => [$packet]], JSON_THROW_ON_ERROR);
    return $receiver->handle('POST', '', static fn(): string => $body, '192.0.2.1', true, 'application/json', 'Bearer ' . $credential);
};
try {
    $first = $send($collector, $token, $event);
    check($first->status === 200, 'new collector discovery failed: ' . $first->body);
    $result = json_decode($first->body, true, flags: JSON_THROW_ON_ERROR)['results'][0];
    check($result['status'] === 'pending', 'must remain pending before adoption');
    check($runtime->live()->count() === 0, 'unadopted data reached journal');
    $sender = $result['sender'];
    $stations = (new ReadModel($path))->stations();
    check(count($stations) === 1 && $stations[0]['id'] === $sender, 'missing from normal station list');
    check($send($collector, bin2hex(random_bytes(32)), $event)->status === 401, 'token takeover allowed');
    check($send('44444444-4444-4444-8444-444444444444', $token, $event)->status === 403, 'token rebound');
    (new Service($path, $now))->execute('station.adopt', ['station' => $sender, 'name' => 'Garden']);
    $accepted = json_decode($send($collector, $token, $event)->body, true, flags: JSON_THROW_ON_ERROR);
    check($accepted['results'][0]['status'] === 'stored', 'adopt did not enable storage');
    check($runtime->live()->count() === 1, 'missing adopted event');
    check(json_decode($send($collector, $token, $event)->body, true)['results'][0]['status'] === 'duplicate', 'retry not idempotent');
    (new Service($path, $now))->execute('station.reject', ['station' => $sender]);
    $event['event_id'] = '33333333-3333-4333-8333-333333333334';
    check(json_decode($send($collector, $token, $event)->body, true)['results'][0]['reason'] === 'station_blocked', 'rejection bypassed');
    $runtime->live()->collector()->enable($collector, false);
    check($send($collector, $token, $event)->status === 401, 'disabled token rediscovered');
    foreach (['55555555-5555-4555-8555-555555555555', '66666666-6666-4666-8666-666666666666'] as $id) {
        check($send($id, bin2hex(random_bytes(32)), $event)->status === 200, 'pending capacity setup');
    }
    check($send('77777777-7777-4777-8777-777777777777', bin2hex(random_bytes(32)), $event)->status === 503, 'pending capacity not enforced');
    print "Discovery, normal Adopt UI, credential binding, rejection, replay and capacity passed.\n";
} finally {
    $runtime->close();
}
