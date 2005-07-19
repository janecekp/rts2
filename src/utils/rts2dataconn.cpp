#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <netdb.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <fcntl.h>

#include "rts2dataconn.h"

Rts2ClientTCPDataConn::Rts2ClientTCPDataConn (Rts2Block * in_master,
					      Rts2Conn * in_owner_conn,
					      char *hostname, int port,
					      int in_totalSize):
Rts2ConnNoSend (in_master)
{
  struct addrinfo hints;
  struct addrinfo *addr;
  char *s_port;
  int ret;

  // delet us if construction of socket fails
  setConnState (CONN_DELETE);
  ownerConnection = in_owner_conn;

  data = NULL;
  dataTop = NULL;
  receivedSize = 0;
  totalSize = in_totalSize;
  // try to resolve hostname..
  hints.ai_flags = 0;
  hints.ai_family = PF_INET;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_protocol = 0;
  asprintf (&s_port, "%i", port);
  ret = getaddrinfo (hostname, s_port, &hints, &addr);
  free (s_port);
  if (ret)
    {
      syslog (LOG_ERR,
	      "Rts2ClientTCPDataConn::Rts2ClientTCPDataConn getaddrinfo: %m");
      freeaddrinfo (addr);
      sock = -1;
      return;
    }
  sock = socket (addr->ai_family, addr->ai_socktype, addr->ai_protocol);
  if (sock == -1)
    {
      syslog (LOG_ERR,
	      "Rts2ClientTCPDataConn::Rts2ClientTCPDataConn socket: %m");
      freeaddrinfo (addr);
      return;
    }
  ret = fcntl (sock, F_SETFL, O_NONBLOCK);
  if (ret)
    {
      syslog (LOG_ERR,
	      "Rts2ClientTCPDataConn::Rts2ClientTCPDataConn cannot set socket non-blocking: %m");
    }
  // try to connect
  ret = connect (sock, addr->ai_addr, addr->ai_addrlen);
  freeaddrinfo (addr);
  data = new char[totalSize];
  dataTop = data;
  if (ret == -1)
    {
      if (errno = EINPROGRESS)
	{
	  setConnState (CONN_CONNECTING);
	  return;
	}
      return;
    }
  setConnState (CONN_CONNECTED);
}

Rts2ClientTCPDataConn::~Rts2ClientTCPDataConn (void)
{
  if (data)
    delete[]data;
}

int
Rts2ClientTCPDataConn::receive (fd_set * set)
{
  size_t data_size = 0;
  if (isConnState (CONN_DELETE))
    return -1;
  if ((sock >= 0) && FD_ISSET (sock, set))
    {
      data_size = read (sock, dataTop, totalSize - receivedSize);
      if (data_size < 0)
	{
	  connectionError ();
	  return -1;
	}
      successfullRead ();
      receivedSize += data_size;
      dataTop += data_size;
    }
  if (receivedSize == totalSize)
    {
      dataReceived ();
      endConnection ();
    }
  return data_size;
}

int
Rts2ClientTCPDataConn::idle ()
{
  if (isConnState (CONN_CONNECTING))
    {
      int err;
      int ret;
      socklen_t len = sizeof (err);

      ret = getsockopt (sock, SOL_SOCKET, SO_ERROR, &err, &len);
      if (ret)
	{
	  syslog (LOG_ERR, "Rts2ConnClient::idle getsockopt %m");
	  connectionError ();
	}
      else if (err)
	{
	  syslog (LOG_ERR, "Rts2ConnClient::idle getsockopt %s",
		  strerror (err));
	  connectionError ();
	}
    }
  return 0;			// we don't want Rts2Conn to take care of our timeouts
}

void
Rts2ClientTCPDataConn::dataReceived ()
{
  ownerConnection->dataReceived (this);
}
